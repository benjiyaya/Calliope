import type { AgentComposerPayload } from '$lib/agentComposer';

const API_BASE = '';

export interface Project {
	id: number;
	title: string;
	idea: string | null;
	genre: string | null;
	tone: string | null;
	target_duration: string | null;
	cover_path: string | null;
	status: string;
	created_at: string;
	updated_at: string;
	stats?: ProjectStats;
}

export interface ProjectStats {
	scene_count: number;
	character_count: number;
	asset_ready_count: number;
	asset_total_count: number;
}

export interface ProjectCreate {
	title: string;
	idea?: string;
	genre?: string;
	tone?: string;
	target_duration?: string;
}

export interface Beat {
	id: number;
	order_index: number;
	title: string;
	description: string | null;
}

export interface Character {
	id: number;
	name: string;
	role: string | null;
	age: string | null;
	appearance: string | null;
	personality: string | null;
	portrait_path: string | null;
	sheet_path: string | null;
	consistency_prompt: string | null;
}

export interface Location {
	id: number;
	name: string;
	description: string | null;
	reference_image_path: string | null;
	consistency_prompt: string | null;
}

export interface Item {
	id: number;
	name: string;
	description: string | null;
	reference_image_path: string | null;
	consistency_prompt: string | null;
}

export interface StoryData {
	project: Project;
	beats: Beat[];
	characters: Character[];
	locations: Location[];
	items: Item[];
}

export interface LlmProfile {
	id: string;
	name: string;
	base_url: string;
	model: string;
	api_key: boolean;
}

export interface Settings {
	host: string;
	port: number;
	data_dir: string;
	assets_dir: string;
	db_name: string;
	llm_base_url: string;
	llm_model: string;
	llm_api_key: boolean;
	llm_profiles: LlmProfile[];
	llm_active_id: string | null;
	comfyui_base_url: string;
	queue_concurrency: number;
	queue_poll_interval_sec: number;
	queue_poll_timeout_sec: number;
	queue_max_retries: number;
	agent_max_steps: number;
	agent_hardening_prompt: string;
	dry_run: boolean;
}

import type { Job, Scene, Workflow, ComfyDynamicInput, ComfyDynamicOutput } from './comfy/types';
export type { Job, Scene, Workflow, ComfyDynamicInput, ComfyDynamicOutput };

async function api<T>(path: string, init?: RequestInit): Promise<T> {
	const res = await fetch(`${API_BASE}${path}`, {
		...init,
		headers: {
			'Content-Type': 'application/json',
			...init?.headers,
		},
	});
	if (!res.ok) {
		const body = await res.text().catch(() => 'unknown error');
		throw new Error(`${res.status}: ${body}`);
	}
	return res.json() as Promise<T>;
}

/** Multipart variant of api() — FormData sets its own Content-Type boundary. */
async function apiUpload<T>(path: string, form: FormData): Promise<T> {
	const res = await fetch(`${API_BASE}${path}`, { method: 'POST', body: form });
	if (!res.ok) {
		const body = await res.text().catch(() => 'unknown error');
		throw new Error(`${res.status}: ${body}`);
	}
	return res.json() as Promise<T>;
}

export function assetUrl(path: string | null | undefined): string | null {
	if (!path) return null;
	return `/api/file?path=${encodeURIComponent(path)}`;
}

export const projects = {
	list: () => api<Project[]>('/api/projects'),
	create: (payload: ProjectCreate) =>
		api<Project>('/api/projects', { method: 'POST', body: JSON.stringify(payload) }),
	get: (id: number) => api<Project & { stats: ProjectStats }>(`/api/projects/${id}`),
	update: (id: number, payload: Partial<ProjectCreate & { status?: string; cover_path?: string | null }>) =>
		api<Project>(`/api/projects/${id}`, { method: 'PATCH', body: JSON.stringify(payload) }),
	delete: (id: number) => api<{ ok: boolean }>(`/api/projects/${id}`, { method: 'DELETE' }),
	getStory: (id: number) => api<StoryData>(`/api/projects/${id}/story`),
	updateBeat: (projectId: number, beatId: number, payload: Partial<Beat>) =>
		api<Beat>(`/api/projects/${projectId}/beats/${beatId}`, {
			method: 'PATCH',
			body: JSON.stringify(payload),
		}),
	updateCharacter: (projectId: number, characterId: number, payload: Partial<Character>) =>
		api<Character>(`/api/projects/${projectId}/characters/${characterId}`, {
			method: 'PATCH',
			body: JSON.stringify(payload),
		}),
	updateLocation: (projectId: number, locationId: number, payload: Partial<Location>) =>
		api<Location>(`/api/projects/${projectId}/locations/${locationId}`, {
			method: 'PATCH',
			body: JSON.stringify(payload),
		}),
	updateItem: (projectId: number, itemId: number, payload: Partial<Item>) =>
		api<Item>(`/api/projects/${projectId}/items/${itemId}`, {
			method: 'PATCH',
			body: JSON.stringify(payload),
		}),
	deleteBeat: (projectId: number, beatId: number) =>
		api<{ ok: boolean }>(`/api/projects/${projectId}/beats/${beatId}`, { method: 'DELETE' }),
	deleteCharacter: (projectId: number, characterId: number) =>
		api<{ ok: boolean }>(`/api/projects/${projectId}/characters/${characterId}`, {
			method: 'DELETE',
		}),
	deleteLocation: (projectId: number, locationId: number) =>
		api<{ ok: boolean }>(`/api/projects/${projectId}/locations/${locationId}`, {
			method: 'DELETE',
		}),
	deleteItem: (projectId: number, itemId: number) =>
		api<{ ok: boolean }>(`/api/projects/${projectId}/items/${itemId}`, {
			method: 'DELETE',
		}),
	getScenes: (id: number) =>
		api<{ scenes: Scene[]; estimated_duration_sec: number }>(`/api/projects/${id}/scenes`),
	updateScene: (projectId: number, sceneId: number, payload: Record<string, unknown>) =>
		api<Scene>(`/api/projects/${projectId}/scenes/${sceneId}`, {
			method: 'PATCH',
			body: JSON.stringify(payload),
		}),
	createScene: (projectId: number, payload: Record<string, unknown>) =>
		api<Scene>(`/api/projects/${projectId}/scenes`, {
			method: 'POST',
			body: JSON.stringify(payload),
		}),
	deleteScene: (projectId: number, sceneId: number) =>
		api<{ ok: boolean }>(`/api/projects/${projectId}/scenes/${sceneId}`, { method: 'DELETE' }),
	reorderScenes: (projectId: number, sceneIds: number[]) =>
		api<{ scenes: Scene[] }>(`/api/projects/${projectId}/scenes/reorder`, {
			method: 'POST',
			body: JSON.stringify({ scene_ids: sceneIds }),
		}),
	generateAssets: (
		id: number,
		payload: {
			missing_only?: boolean;
			character_ids?: number[];
			location_ids?: number[];
			item_ids?: number[];
			workflow_id?: number;
			input_values?: Record<string, unknown>;
			asset_target?: 'sheet';
			prompt?: string;
		} = {},
	) =>
		api<{ ok: boolean; jobs: Job[] }>(`/api/projects/${id}/generate-assets`, {
			method: 'POST',
			body: JSON.stringify(payload),
		}),
	getAssets: (id: number) =>
		api<{ characters: Character[]; locations: Location[]; items: Item[] }>(
			`/api/projects/${id}/assets`,
		),
};

export const settings = {
	get: () => api<Settings>('/api/settings'),
	update: (payload: Record<string, unknown>) =>
		api<Settings>('/api/settings', { method: 'POST', body: JSON.stringify(payload) }),
};

export const workflows = {
	list: () => api<Workflow[]>('/api/workflows'),
	analyze: (workflow_json: Record<string, unknown>) =>
		api<{
			inputs: ComfyDynamicInput[];
			outputs: ComfyDynamicOutput[];
			suggested_profile?: string;
			motion_role?: 'first' | 'next' | null;
		}>('/api/workflows/analyze', {
			method: 'POST',
			body: JSON.stringify({ workflow_json }),
		}),
	create: (payload: {
		name: string;
		kind: 'image' | 'video';
		workflow_json: Record<string, unknown>;
		description?: string;
		prompt_profile?: string;
	}) => api<Workflow>('/api/workflows', { method: 'POST', body: JSON.stringify(payload) }),
	update: (
		id: number,
		payload: Partial<{
			name: string;
			kind: string;
			is_enabled: boolean;
			description: string;
			prompt_profile: string;
		}>,
	) => api<Workflow>(`/api/workflows/${id}`, { method: 'PATCH', body: JSON.stringify(payload) }),
	delete: (id: number) => api<{ ok: boolean }>(`/api/workflows/${id}`, { method: 'DELETE' }),
	get: (id: number) => api<Workflow>(`/api/workflows/${id}`),
};

export const jobsApi = {
	list: (projectId?: number) =>
		api<Job[]>(`/api/jobs${projectId != null ? `?project_id=${projectId}` : ''}`),
	retry: (id: number) => api<Job>(`/api/jobs/${id}/retry`, { method: 'POST' }),
	cancel: (id: number) => api<{ ok: boolean }>(`/api/jobs/${id}/cancel`, { method: 'POST' }),
	pause: () => api<{ ok: boolean; paused: boolean }>('/api/jobs/pause', { method: 'POST' }),
	resume: () => api<{ ok: boolean; paused: boolean }>('/api/jobs/resume', { method: 'POST' }),
	queueStatus: () => api<{ paused: boolean }>('/api/jobs/queue-status'),
	generateVideos: (
		projectId: number,
		payload: {
			scene_ids?: number[];
			workflow_id?: number;
			input_values?: Record<string, unknown>;
			continue_motion?: boolean;
		} = {},
	) =>
		api<{ ok: boolean; jobs: Job[] }>(`/api/jobs/projects/${projectId}/generate-videos`, {
			method: 'POST',
			body: JSON.stringify(payload),
		}),
	exportFilm: (projectId: number) =>
		api<{ ok: boolean; job: Job }>(`/api/jobs/projects/${projectId}/export`, { method: 'POST' }),
};

export type PlaygroundAttachTarget = 'character_sheet' | 'location' | 'item' | 'scene';

export type UploadKind = 'image' | 'video' | 'audio';

export interface PlaygroundUpload {
	ok: boolean;
	path: string;
	name: string;
	kind: UploadKind;
}

export interface PlaygroundUploadListItem {
	name: string;
	path: string;
	kind: UploadKind;
	size: number;
	mtime: string;
}

export const playgroundApi = {
	project: () => api<Project>('/api/playground/project'),
	jobs: () => api<Job[]>('/api/playground/jobs'),
	generate: (payload: { workflow_id: number; input_values?: Record<string, unknown> }) =>
		api<{ ok: boolean; job: Job }>('/api/playground/generate', {
			method: 'POST',
			body: JSON.stringify(payload),
		}),
	upload: (file: File) => {
		const form = new FormData();
		form.append('file', file);
		return apiUpload<PlaygroundUpload>('/api/playground/uploads', form);
	},
	listUploads: () => api<PlaygroundUploadListItem[]>('/api/playground/uploads'),
	deleteJob: (jobId: number) =>
		api<{ ok: boolean; job_id: number; deleted_files: string[]; missing_files: string[] }>(
			`/api/playground/jobs/${jobId}`,
			{ method: 'DELETE' },
		),
	attach: (payload: {
		path: string;
		project_id: number;
		target: PlaygroundAttachTarget;
		character_id?: number;
		location_id?: number;
		item_id?: number;
		scene_id?: number;
		name?: string;
	}) =>
		api<{ ok: boolean; path: string; target: string; project_id: number; item_id?: number }>(
			'/api/playground/attach',
			{ method: 'POST', body: JSON.stringify(payload) },
		),
};

// ── Agents (agentic harness) ────────────────────────────────────────────

export interface AgentSession {
	id: number;
	project_id: number | null;
	title: string;
	status: 'idle' | 'running' | 'error';
	created_at: string;
	updated_at: string;
	running?: boolean;
	project?: { id: number; title: string; status: string } | null;
}

export interface AgentMessage {
	id: number;
	session_id: number;
	role: 'user' | 'assistant' | 'tool' | 'system';
	content: string;
	agent_name?: string | null;
	tool_name?: string | null;
	tool_args?: Record<string, unknown> | null;
	tool_result?: unknown;
	status?: string | null;
	reasoning?: string | null;
	created_at: string;
	mentions?: AgentComposerPayload['mentions'];
	attachments?: AgentComposerPayload['attachments'];
}

export interface AgentTask {
	role: string;
	goal: string;
	status: 'pending' | 'running' | 'done' | 'failed';
	index?: number;
}

export interface AgentPlan {
	tasks: AgentTask[];
	note?: string | null;
}

export const agentApi = {
	listSessions: (projectId?: number) =>
		api<AgentSession[]>(
			`/api/agent/sessions${projectId != null ? `?project_id=${projectId}` : ''}`,
		),
	listLinkableProjects: () =>
		api<{ id: number; title: string; status: string }[]>('/api/agent/projects'),
	createSession: (payload: { title?: string; project_id?: number | null }) =>
		api<AgentSession>('/api/agent/sessions', {
			method: 'POST',
			body: JSON.stringify(payload),
		}),
	getSession: (id: number) =>
		api<AgentSession & { messages: AgentMessage[]; plan?: AgentPlan | null }>(
			`/api/agent/sessions/${id}`,
		),
	patchSession: (
		id: number,
		payload: { title?: string; project_id?: number | null; unlink?: boolean },
	) =>
		api<AgentSession>(`/api/agent/sessions/${id}`, {
			method: 'PATCH',
			body: JSON.stringify(payload),
		}),
	deleteSession: (id: number) =>
		api<{ ok: boolean }>(`/api/agent/sessions/${id}`, { method: 'DELETE' }),
	postMessage: (id: number, payload: AgentComposerPayload | string) => {
		const body =
			typeof payload === 'string'
				? { content: payload, mentions: [], attachments: [] }
				: payload;
		return api<{ ok: boolean; message: AgentMessage }>(`/api/agent/sessions/${id}/messages`, {
			method: 'POST',
			body: JSON.stringify(body),
		});
	},
	cancel: (id: number) =>
		api<{ ok: boolean }>(`/api/agent/sessions/${id}/cancel`, { method: 'POST' }),
};
