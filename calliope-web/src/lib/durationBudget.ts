/**
 * Mirror of calliope.agent.prompts duration helpers — keep in sync.
 * Used so Story UI can show the beat budget before drafting.
 */

export function estimateTargetSeconds(targetDuration: string | null | undefined): number {
	const text = (targetDuration || '').trim().toLowerCase();
	if (!text) return 30;

	const scenesM = text.match(/(\d+)\s*scenes?/);
	if (scenesM && !/min|sec/.test(text)) {
		return Math.max(30, Number(scenesM[1]) * 8);
	}

	let minutes = 0;
	let seconds = 0;
	for (const m of text.matchAll(/(\d+(?:\.\d+)?)\s*(min|mins|minutes?|m)\b/g)) {
		minutes += Number(m[1]);
	}
	for (const m of text.matchAll(/(\d+(?:\.\d+)?)\s*(sec|secs|seconds?|s)\b/g)) {
		seconds += Number(m[1]);
	}
	if (minutes || seconds) return Math.max(15, Math.round(minutes * 60 + seconds));

	const bare = text.match(/\b(\d+(?:\.\d+)?)\b/);
	if (bare) {
		const n = Number(bare[1]);
		if (text.includes('medium') || n >= 2) {
			if (n <= 30) return Math.round(n * 60);
		}
		if (n <= 180) return Math.round(n);
	}

	if (text.includes('medium')) return 120;
	if (text.includes('short') || text.includes('brief')) return 30;
	if (text.includes('long') || text.includes('feature')) return 180;
	return 60;
}

/** ~12s narrative weight per beat; capped at 60. */
export function recommendBeatCount(targetDuration: string | null | undefined): number {
	const secs = estimateTargetSeconds(targetDuration);
	return Math.max(4, Math.min(60, Math.round(secs / 12)));
}

export function recommendSceneCount(targetDuration: string | null | undefined): number {
	const secs = estimateTargetSeconds(targetDuration);
	return Math.max(4, Math.min(90, Math.round(secs / 7)));
}

/** Same user prompt the backend sends to the LLM (for transparency). */
export function buildStoryDraftPromptPreview(args: {
	title: string;
	idea: string;
	genre: string;
	tone: string;
	targetDuration: string;
}): string {
	const secs = estimateTargetSeconds(args.targetDuration);
	const beatN = recommendBeatCount(args.targetDuration);
	return `Create a story brief for an AI-generated video.

required_beat_count: ${beatN}
estimated_runtime_seconds: ${secs}
target_duration_text: ${args.targetDuration || 'short (~30 seconds)'}

Title: ${args.title || 'Untitled'}
Idea: ${args.idea || 'No idea provided.'}
Genre: ${args.genre || 'Not specified'}
Tone: ${args.tone || 'cinematic, atmospheric'}

=== HARD CONSTRAINTS (non-negotiable) ===
1. The JSON field "beats" MUST contain EXACTLY ${beatN} objects.
2. order_index must run 1..${beatN} with no gaps.
3. Do NOT compress a long runtime into a handful of beats.
4. ~${secs}s runtime → fine-grained beats (~12s story weight each).

(+ JSON schema for title, logline, beats, characters, locations)

FINAL CHECK: beats.length == ${beatN}`;
}
