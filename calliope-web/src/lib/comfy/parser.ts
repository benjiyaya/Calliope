import type { ComfyDynamicInput, ComfyDynamicOutput, WorkflowNode } from './types';

const TEXT_AREA = new Set([
	'CLIPTextEncode',
	'Note',
	'ShowText',
	'ImpactWildcardProcessor',
	'PrimitiveStringMultiline',
	'PrimitiveString',
]);
const NUMBER = new Set(['INT', 'FLOAT', 'PrimitiveInt', 'PrimitiveFloat', 'KSampler', 'KSamplerAdvanced']);
const IMAGE = new Set(['LoadImage', 'ImageLoader', 'ETN_LoadImageBase64']);
const IMAGE_URL = new Set(['Load Image From Url (mtb)']);
const AUDIO = new Set(['LoadAudio', 'VHS_LoadAudio']);
const VIDEO = new Set(['LoadVideo', 'VHS_LoadVideo', 'VHS_LoadVideoPath']);
const VIDEO_OUT = new Set(['VHS_VideoCombine', 'SaveVideo', 'VideoOutput', 'AnimateDiffCombine']);
const IMAGE_OUT = new Set(['SaveImage', 'PreviewImage', 'SaveImageWebsocket', 'ETN_SendImageWebSocket']);

/** Matches (Input), (Input:prompt), (Output:image), … */
const TITLE_TAG_RE = /\((Input|Output)(?::([a-zA-Z0-9_-]+))?\)/i;

const INPUT_ROLE_ALIASES: Record<string, string[]> = {
	prompt: ['prompt', 'positive'],
	negative: ['negative', 'neg'],
	width: ['width', 'w'],
	height: ['height', 'h'],
	character: ['character', 'char', 'portrait', 'sheet', 'face', 'ref'],
	location: ['location', 'loc', 'environment', 'env', 'background', 'scene'],
	image: ['image', 'img'],
	video: ['video', 'vid'],
	audio: ['audio', 'sound', 'sfx'],
	seed: ['seed'],
	duration: ['duration', 'dur', 'length', 'seconds'],
};

const OUTPUT_ROLE_ALIASES: Record<string, string[]> = {
	image: ['image', 'img'],
	video: ['video', 'vid'],
};

export function parseTitleTag(title: string): {
	kind: 'input' | 'output' | null;
	role: string | null;
	label: string;
} {
	if (!title) return { kind: null, role: null, label: '' };
	const match = TITLE_TAG_RE.exec(title);
	if (!match) return { kind: null, role: null, label: title.trim() };
	const kind = match[1].toLowerCase() as 'input' | 'output';
	const role = match[2] ? match[2].toLowerCase() : null;
	const stripped = title.replace(TITLE_TAG_RE, '').trim();
	const label = stripped || role || kind;
	return { kind, role, label };
}

export function normalizeInputRole(role: string | null | undefined): string | null {
	if (!role) return null;
	const r = role.toLowerCase().trim();
	for (const [canonical, aliases] of Object.entries(INPUT_ROLE_ALIASES)) {
		if (r === canonical || aliases.includes(r)) return canonical;
	}
	return r;
}

export function normalizeOutputRole(role: string | null | undefined): string | null {
	if (!role) return null;
	const r = role.toLowerCase().trim();
	for (const [canonical, aliases] of Object.entries(OUTPUT_ROLE_ALIASES)) {
		if (r === canonical || aliases.includes(r)) return canonical;
	}
	return r;
}

function classToInputKind(classType: string): ComfyDynamicInput['kind'] {
	if (IMAGE.has(classType)) return 'image';
	if (IMAGE_URL.has(classType)) return 'image_url';
	if (AUDIO.has(classType)) return 'audio';
	if (VIDEO.has(classType)) return 'video';
	if (NUMBER.has(classType)) return 'number';
	if (TEXT_AREA.has(classType)) return 'textarea';
	const lower = classType.toLowerCase();
	if (lower.includes('video')) return 'video';
	if (lower.includes('audio')) return 'audio';
	if (lower.includes('image') || lower.includes('load')) return 'image';
	if (lower.includes('int') || lower.includes('float') || lower.includes('seed')) return 'number';
	if (lower.includes('text') || lower.includes('clip') || lower.includes('prompt')) return 'textarea';
	return 'text';
}

function extractDefault(node: WorkflowNode): string | number | undefined {
	const { inputs, class_type } = node;
	if (IMAGE.has(class_type) || AUDIO.has(class_type) || VIDEO.has(class_type)) return undefined;
	if (typeof inputs.text === 'string') return inputs.text;
	if (typeof inputs.value === 'string' || typeof inputs.value === 'number') return inputs.value;
	if (typeof inputs.int === 'number') return inputs.int;
	if (typeof inputs.float === 'number') return inputs.float;
	return undefined;
}

export function parseDynamicInputs(workflow: Record<string, WorkflowNode>): ComfyDynamicInput[] {
	const results: ComfyDynamicInput[] = [];
	for (const [nodeId, node] of Object.entries(workflow)) {
		const title = node._meta?.title ?? '';
		const { kind, role, label } = parseTitleTag(title);
		if (kind !== 'input') continue;
		results.push({
			nodeId,
			label: label || node.class_type,
			role: normalizeInputRole(role),
			kind: classToInputKind(node.class_type),
			defaultValue: extractDefault(node),
			required: true,
		});
	}
	return results;
}

export function parseDynamicOutputs(workflow: Record<string, WorkflowNode>): ComfyDynamicOutput[] {
	const results: ComfyDynamicOutput[] = [];
	for (const [nodeId, node] of Object.entries(workflow)) {
		const title = node._meta?.title ?? '';
		const { kind, role, label } = parseTitleTag(title);
		if (kind !== 'output') continue;
		const canon = normalizeOutputRole(role);
		let outKind: ComfyDynamicOutput['kind'] = 'other';
		if (canon === 'video' || VIDEO_OUT.has(node.class_type) || node.class_type.toLowerCase().includes('video'))
			outKind = 'video';
		else if (
			canon === 'image' ||
			IMAGE_OUT.has(node.class_type) ||
			node.class_type.toLowerCase().includes('image')
		)
			outKind = 'image';
		results.push({
			nodeId,
			label: label || node.class_type,
			role: canon,
			kind: outKind,
		});
	}
	return results;
}
