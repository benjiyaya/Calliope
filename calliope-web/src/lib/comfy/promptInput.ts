import type { ComfyDynamicInput } from './types';
import { normalizeInputRole } from './parser';

const PROMPT_ROLES = new Set(['prompt', 'positive']);

function roleOf(inp: ComfyDynamicInput): string | null {
	return normalizeInputRole(inp.role ?? null);
}

/** Positive prompt input — prefers (Input:prompt), legacy label fallback. */
export function isPromptLikeInput(inp: ComfyDynamicInput): boolean {
	const role = roleOf(inp);
	if (role && PROMPT_ROLES.has(role)) return true;
	if (role) return false; // other roles are never "the" prompt
	if (inp.kind !== 'text' && inp.kind !== 'textarea') return false;
	const label = (inp.label || '').toLowerCase();
	if (label.includes('negative')) return false;
	return (
		label.includes('prompt') ||
		label.includes('positive') ||
		label.includes('text') ||
		inp.kind === 'textarea'
	);
}

/** Drop prompt-role fields from shared workflow forms (entity Image prompt owns those). */
export function withoutPromptInputs(inputs: ComfyDynamicInput[] | undefined | null): ComfyDynamicInput[] {
	if (!inputs?.length) return [];
	return inputs.filter((inp) => !isPromptLikeInput(inp));
}

/** True if this workflow schema has a prompt-role (or legacy prompt-like) (Input). */
export function workflowHasPromptInput(inputs: ComfyDynamicInput[] | undefined | null): boolean {
	if (!inputs?.length) return false;
	return inputs.some(isPromptLikeInput);
}

/** Strip blank strings so empty fields cannot wipe smart-fill. */
export function compactInputValues(
	values: Record<string, unknown> | undefined | null,
): Record<string, unknown> {
	if (!values) return {};
	const out: Record<string, unknown> = {};
	for (const [k, v] of Object.entries(values)) {
		if (v === null || v === undefined) continue;
		if (typeof v === 'string' && !v.trim()) continue;
		out[k] = v;
	}
	return out;
}
