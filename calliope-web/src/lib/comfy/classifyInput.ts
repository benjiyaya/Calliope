/**
 * Omni Composer input classification.
 *
 * Maps each ComfyDynamicInput — based on its canonical role — to a UI zone
 * and widget type inside the Omni layout. This is presentation-only: the
 * underlying values map (nodeId → value) is identical to what
 * ComfyDynamicForm would produce.
 *
 * Zones:
 *   composer  → main prompt textarea + collapsible negative
 *   media     → thumbnail tiles (upload / asset-pick)
 *   control   → compact pills (resolution, duration, seed)
 *   advanced  → hidden inside an "Advanced" popover
 */
import type { ComfyDynamicInput } from './types';
import { normalizeInputRole } from './parser';

export type InputZone = 'composer' | 'media' | 'control' | 'advanced' | 'hidden';

export type OmniWidget =
	| 'promptTextarea'
	| 'negativeTextarea'
	| 'uploadTile'
	| 'assetTile'
	| 'resolutionPill'
	| 'numberPill'
	| 'textPill';

export interface ClassifiedInput {
	input: ComfyDynamicInput;
	zone: InputZone;
	widget: OmniWidget;
}

/**
 * Classify a single input by its canonical role.
 *
 * The rules are deliberately exhaustive — every input lands in exactly one
 * zone. Unknown roles fall to `advanced` so no field is ever hidden or lost.
 */
export function classifyInput(inp: ComfyDynamicInput): ClassifiedInput {
	const role = normalizeInputRole(inp.role);

	// --- Composer ---
	if (role === 'prompt' || role === 'positive') {
		return { input: inp, zone: 'composer', widget: 'promptTextarea' };
	}
	if (role === 'negative') {
		return { input: inp, zone: 'composer', widget: 'negativeTextarea' };
	}

	// --- Media tray ---
	if (
		role === 'character' ||
		role === 'location' ||
		role === 'image' ||
		role === 'video' ||
		role === 'audio'
	) {
		const isMediaKind =
			inp.kind === 'image' ||
			inp.kind === 'image_url' ||
			inp.kind === 'audio' ||
			inp.kind === 'video';
		return {
			input: inp,
			zone: 'media',
			widget: isMediaKind ? 'uploadTile' : 'assetTile',
		};
	}

	// --- Control bar ---
	if (role === 'width' || role === 'height') {
		return { input: inp, zone: 'control', widget: 'resolutionPill' };
	}
	if (role === 'duration' || role === 'seed') {
		return { input: inp, zone: 'control', widget: 'numberPill' };
	}

	// --- Advanced popover ---
	if (inp.kind === 'number') {
		return { input: inp, zone: 'advanced', widget: 'numberPill' };
	}
	return { input: inp, zone: 'advanced', widget: 'textPill' };
}

/**
 * Batch-classify an entire input schema into grouped buckets.
 *
 * Resolution pairs (width + height in the same schema) are detected so the
 * OmniComposer can render a single "Resolution" pill that writes both
 * nodeIds on change.
 */
export function classifyAll(inputs: ComfyDynamicInput[]): {
	prompt: ClassifiedInput | null;
	negative: ClassifiedInput | null;
	media: ClassifiedInput[];
	control: ClassifiedInput[];
	advanced: ClassifiedInput[];
	/** Non-null when both width AND height are present → merge into one pill. */
	resolutionPair: { width: ClassifiedInput; height: ClassifiedInput } | null;
} {
	const classified = inputs.map(classifyInput);

	const prompt = classified.find((c) => c.widget === 'promptTextarea') ?? null;
	const negative = classified.find((c) => c.widget === 'negativeTextarea') ?? null;
	const media = classified.filter((c) => c.zone === 'media');
	const advanced = classified.filter((c) => c.zone === 'advanced');

	// Separate resolution from other control inputs
	const controlNonRes = classified.filter(
		(c) => c.zone === 'control' && c.widget !== 'resolutionPill',
	);
	const resInputs = classified.filter((c) => c.widget === 'resolutionPill');

	// Detect width + height pair
	const wInput = resInputs.find((c) => normalizeInputRole(c.input.role) === 'width');
	const hInput = resInputs.find((c) => normalizeInputRole(c.input.role) === 'height');
	const resolutionPair =
		wInput && hInput ? { width: wInput, height: hInput } : null;

	// If only one of width/height exists, treat it as a standalone control number pill
	const standaloneRes = resolutionPair ? [] : resInputs;

	return {
		prompt,
		negative,
		media,
		control: [...controlNonRes, ...standaloneRes],
		advanced,
		resolutionPair,
	};
}

// ── Resolution presets ────────────────────────────────────────────────────────

export interface ResolutionPreset {
	label: string;
	width: number;
	height: number;
}

export const RESOLUTION_PRESETS: ResolutionPreset[] = [
	{ label: '720p', width: 1280, height: 720 },
	{ label: '1080p', width: 1920, height: 1080 },
];

/**
 * Given current width/height values, find the matching preset label
 * (or return null for custom / unset values).
 */
export function resolutionLabel(
	width: number | string | undefined,
	height: number | string | undefined,
): string | null {
	const w = Number(width);
	const h = Number(height);
	if (!w || !h) return null;
	const match = RESOLUTION_PRESETS.find((p) => p.width === w && p.height === h);
	return match?.label ?? `${w}×${h}`;
}
