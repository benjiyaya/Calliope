export type ComfyInputKind = 'text' | 'textarea' | 'number' | 'image' | 'image_url' | 'audio' | 'video';
export type ComfyOutputKind = 'image' | 'video' | 'other';

export interface ComfyDynamicInput {
	nodeId: string;
	label: string;
	/** Role from title tag, e.g. (Input:prompt) → "prompt". Null for plain (Input). */
	role?: string | null;
	kind: ComfyInputKind;
	defaultValue?: string | number;
	required: boolean;
}

export interface ComfyDynamicOutput {
	nodeId: string;
	label: string;
	/** Role from title tag, e.g. (Output:image) → "image". */
	role?: string | null;
	kind: ComfyOutputKind;
}

export interface WorkflowNode {
	class_type: string;
	inputs: Record<string, unknown>;
	_meta?: { title?: string };
}

export interface Workflow {
	id: number;
	name: string;
	kind: 'image' | 'video';
	workflow_json: Record<string, WorkflowNode>;
	input_schema: ComfyDynamicInput[];
	output_schema: ComfyDynamicOutput[];
	description: string | null;
	prompt_profile: string;
	is_enabled: boolean;
}

export interface Job {
	id: number;
	project_id: number;
	scene_id: number | null;
	kind: string;
	workflow_id: number | null;
	status: string;
	payload: Record<string, unknown>;
	output_paths: string[];
	error: string | null;
	created_at: string;
	started_at: string | null;
	completed_at: string | null;
	retry_count: number;
}

export interface SceneVideoSettings {
	input_values?: Record<string, string | number>;
	/** 'auto' | 'upload' | target scene id as string. */
	clip_source?: string;
	form_workflow_id?: number;
	prompt_draft?: string;
	prompt_draft_meta?: { based_on?: string; saved_at?: string };
	[key: string]: unknown;
}

export interface Scene {
	id: number;
	project_id: number;
	order_index: number;
	heading: string | null;
	action: string | null;
	dialog: string | null;
	duration_sec: number | null;
	workflow_id: number | null;
	env_image_path: string | null;
	location_id: number | null;
	video_path: string | null;
	chain_from_prev?: number | boolean | null;
	video_settings?: SceneVideoSettings | null;
	character_ids: number[];
	characters: Array<{
		id: number;
		name: string;
		role: string | null;
		portrait_path: string | null;
		sheet_path: string | null;
	}>;
}
