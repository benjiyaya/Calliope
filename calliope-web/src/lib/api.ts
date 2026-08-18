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

export interface StoryData {
	project: Project;
	beats: Beat[];
	characters: Character[];
	locations: Location[];
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
	comfyui_base_url: string;
	queue_concurrency: number;
	queue_poll_interval_sec: number;
	queue_max_retries: number;
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
	generateStory: (id: number) =>
		api<{ ok: boolean; generated: unknown }>(`/api/projects/${id}/generate-story`, {
			method: 'POST',
		}),
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
	getScenes: (id: number) =>
		api<{ scenes: Scene[]; estimated_duration_sec: number }>(`/api/projects/${id}/scenes`),
	generateScript: (id: number, opts?: { scene_count?: number }) =>
		api<{ ok: boolean; scenes: Scene[] }>(`/api/projects/${id}/generate-script`, {
			method: 'POST',
			body: JSON.stringify({
				replace: true,
				...(opts?.scene_count != null ? { scene_count: opts.scene_count } : {}),
			}),
		}),
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
		api<{ characters: Character[]; locations: Location[] }>(`/api/projects/${id}/assets`),
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
		} = {},
	) =>
		api<{ ok: boolean; jobs: Job[] }>(`/api/jobs/projects/${projectId}/generate-videos`, {
			method: 'POST',
			body: JSON.stringify(payload),
		}),
	exportFilm: (projectId: number) =>
		api<{ ok: boolean; job: Job }>(`/api/jobs/projects/${projectId}/export`, { method: 'POST' }),
};

export type PlaygroundAttachTarget = 'character_sheet' | 'location' | 'scene';

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
		scene_id?: number;
	}) =>
		api<{ ok: boolean; path: string; target: string; project_id: number }>(
			'/api/playground/attach',
			{ method: 'POST', body: JSON.stringify(payload) },
		),
};
