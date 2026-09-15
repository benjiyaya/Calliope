<script lang="ts">
	import { createMutation, createQuery, useQueryClient } from '@tanstack/svelte-query';
	import { toStore } from 'svelte/store';
	import { toast } from '$lib/toast';
	import { t } from '$lib/i18n.svelte';
	import {
		assetUrl,
		jobsApi,
		playgroundApi,
		projects,
		workflows,
		type Clip,
		type Job,
		type Scene,
		type Workflow,
	} from '$lib/api';
	import { compactInputValues } from '$lib/comfy/promptInput';
	import { createUploadManager } from '$lib/comfy/useUpload.svelte';
	import { normalizeInputRole } from '$lib/comfy/parser';
	import type { ComfyDynamicInput } from '$lib/comfy/types';
	import type { AssetOption } from '$lib/assetPicker';
	import { shotApi } from '$lib/shot/api';
	import { progressFor } from '$lib/jobProgress';
	import SafeMedia from './SafeMedia.svelte';
	import PromptPreviewModal from './video/PromptPreviewModal.svelte';
	import VideoEditWorkspace from './video/VideoEditWorkspace.svelte';
	import Button from './ui/Button.svelte';
	import Icon from './ui/Icon.svelte';
	import ProgressBar from './ui/ProgressBar.svelte';
	import Spinner from './ui/Spinner.svelte';
	import StatusChip from './ui/StatusChip.svelte';

	interface Props {
		projectId: number;
		projectTitle?: string;
	}

	let { projectId, projectTitle = 'film' }: Props = $props();
	const client = useQueryClient();

	let view = $state<'edit' | 'film'>('edit');
	/** Selected render unit = a clip id (flattened playback order). */
	let selectedClipId = $state<number | null>(null);
	let formValues = $state<Record<string, string | number>>({});
	let selectedWorkflow = $state<Record<number, number>>({});
	let lastFormScene = $state<number | null>(null);
	// Clip source for continue scenes, keyed per scene: 'auto' | 'upload' | scene id.
	let clipSource = $state<Record<number, string>>({});
	// Hidden file input backing "Upload file" in the video source modal.
	let videoFileInput = $state<HTMLInputElement | null>(null);
	const videoUploadMgr = createUploadManager();

	async function onVideoFileChosen(e: Event) {
		const el = e.currentTarget as HTMLInputElement;
		const file = el.files?.[0];
		el.value = '';
		const node = videoInputNodeFor(selected ? workflowFor(selected) : undefined);
		if (!file || !selected || !node) return;
		const path = await videoUploadMgr.uploadSafe(node.nodeId, file);
		if (path) {
			formValues = { ...formValues, [node.nodeId]: path };
		}
	}

	// Per-scene input cache — switching scenes no longer wipes the form.
	const formCache = new Map<number, Record<string, string | number>>();

	const scenesQuery = createQuery(
		toStore(() => ({
			queryKey: ['scenes', projectId],
			queryFn: () => projects.getScenes(projectId),
		})),
	);
	const assetsQuery = createQuery(
		toStore(() => ({
			queryKey: ['assets', projectId],
			queryFn: () => projects.getAssets(projectId),
		})),
	);
	const workflowsQuery = createQuery({
		queryKey: ['workflows'],
		queryFn: workflows.list,
	});
	const jobsQuery = createQuery(
		toStore(() => ({
			queryKey: ['jobs', projectId],
			queryFn: () => jobsApi.list(projectId),
		})),
	);
	// Refreshed via SSE invalidation from the project page (job.* events) and
	// by the pause/resume buttons below — no polling.
	const queueStatusQuery = createQuery({
		queryKey: ['queue-status'],
		queryFn: jobsApi.queueStatus,
	});
	const uploadsQuery = createQuery({
		queryKey: ['playground-uploads'],
		queryFn: playgroundApi.listUploads,
	});
	const shotCapturesQuery = createQuery({
		queryKey: ['shot-captures-all'],
		queryFn: async () => {
			const comps = await shotApi.listCompositions();
			const pages = await Promise.all(comps.map((c) => shotApi.listCaptures(c.id)));
			return pages.flatMap((caps, i) =>
				caps.map((cap) => ({ ...cap, compTitle: comps[i].title })),
			);
		},
	});

	const scenes = $derived(($scenesQuery.data?.scenes ?? []) as Scene[]);
	/** Flattened clips in playback order — the renderable units. */
	const allClips = $derived(
		scenes.flatMap((s) => (s.clips ?? []).map((c) => ({ clip: c, scene: s }))),
	);
	const totalSec = $derived(
		allClips.length > 0
			? allClips.reduce((sum, { clip }) => sum + Math.max(clip.duration_sec || 5, 1), 0)
			: scenes.reduce((sum, s) => sum + Math.max(s.duration_sec || 5, 1), 0),
	);

	/** Flattened filmstrip entries with per-scene shot index + `#3.2` label. */
	const filmClips = $derived(
		allClips.map(({ clip, scene }) => {
			const idx = (scene.clips ?? []).findIndex((c) => c.id === clip.id);
			const index = idx < 0 ? 0 : idx;
			return { clip, scene, index, label: clipLabel(scene, index) };
		}),
	);

	const assetOptions = $derived.by(() => {
		const chars = $assetsQuery.data?.characters ?? [];
		const locs = $assetsQuery.data?.locations ?? [];
		const items = $assetsQuery.data?.items ?? [];
		const opts: AssetOption[] = [];
		for (const c of chars) {
			if (c.sheet_path) {
				opts.push({
					label: `${c.name} · sheet`,
					path: c.sheet_path,
					kind: 'image',
					group: 'character',
				});
			}
		}
		for (const loc of locs) {
			if (loc.reference_image_path) {
				opts.push({
					label: `${loc.name} · environment`,
					path: loc.reference_image_path,
					kind: 'image',
					group: 'location',
				});
			}
		}
		for (const it of items) {
			if (it.reference_image_path) {
				opts.push({
					label: `${it.name} · item`,
					path: it.reference_image_path,
					kind: 'image',
					group: 'item',
				});
			}
		}
		for (const sc of scenes) {
			if (sc.video_path) {
				opts.push({
					label: `${t('queue.clipLabel', { n: sc.order_index })} · ${sc.heading || t('queue.sceneLabel')}`,
					path: sc.video_path,
					kind: 'video',
					group: 'clip',
				});
			}
		}
		for (const up of $uploadsQuery.data ?? []) {
			// Documents are agent-context files, not Comfy job inputs — never
			// offer them in a reference picker.
			if (up.kind === 'document') continue;
			opts.push({ label: `${up.name} · ${t('queue.uploadSuffix')}`, path: up.path, kind: up.kind, group: 'upload' });
		}
		for (const cap of $shotCapturesQuery.data ?? []) {
			if (!cap.file_path) continue;
			if (cap.kind === 'video') {
				opts.push({
					label: `${cap.compTitle} · ${cap.label || t('queue.blockoutLabel')} · ${t('queue.blockoutClipSuffix')}`,
					path: cap.file_path,
					kind: 'video',
					group: 'shot',
				});
			} else if (cap.kind === 'image') {
				opts.push({
					label: `${cap.compTitle} · ${cap.label || t('queue.blockoutLabel')} · ${t('queue.blockoutSuffix')}`,
					path: cap.file_path,
					kind: 'image',
					group: 'shot',
				});
			}
		}
		return opts;
	});

	const videoWorkflows = $derived(
		(($workflowsQuery.data ?? []) as Workflow[]).filter((w) => w.is_enabled && w.kind === 'video'),
	);
	const enabledWorkflows = $derived(
		videoWorkflows.length > 0
			? videoWorkflows
			: (($workflowsQuery.data ?? []) as Workflow[]).filter((w) => w.is_enabled),
	);

	$effect(() => {
		if (selectedClipId != null && allClips.some((c) => c.clip.id === selectedClipId)) return;
		selectedClipId = allClips[0]?.clip.id ?? null;
	});

	/** The flattened filmstrip entry (clip + scene + label) for the current selection. */
	const selectedEntry = $derived(
		filmClips.find((c) => c.clip.id === selectedClipId) ?? null,
	);
	const selected = $derived(selectedEntry?.scene ?? null);

	$effect(() => {
		const id = selectedClipId ?? null;
		if (id === lastFormScene) return;
		if (lastFormScene != null) formCache.set(lastFormScene, formValues);
		lastFormScene = id;
		if (id != null) {
			const entry = filmClips.find((c) => c.clip.id === id);
			if (!entry) return;
			const stored = formCache.get(id);
			if (stored) {
				formValues = { ...stored };
			} else if (entry.clip.video_settings?.input_values) {
				// First open of a persisted clip setup: hydrate from the clip row.
				formValues = {
					...seedClipDefaults(entry),
					...entry.clip.video_settings.input_values,
				};
			} else if (entry.scene.video_settings?.input_values) {
				// Legacy un-expanded clip: scene row carries the setup.
				formValues = {
					...seedClipDefaults(entry),
					...entry.scene.video_settings.input_values,
				};
			} else {
				formValues = { ...seedClipDefaults(entry) };
			}
		} else {
			formValues = {};
		}
	});

	// --- Auto-save scene setup (issue #28) ---
	// Debounced write-through: in-memory formCache stays source of truth for
	// the session; the scene row makes it survive restarts. Hash-compare so
	// polling refetches never trigger PATCH churn.
	let saveTimer: ReturnType<typeof setTimeout> | null = null;
	let lastSavedHash = $state('');

	function currentClipSettings(): Record<string, unknown> {
		if (!selectedEntry) return {};
		const out: Record<string, unknown> = {
			input_values: compactInputValues(formValues),
		};
		const wfId = selectedWorkflow[selectedEntry.scene.id];
		if (wfId) out.form_workflow_id = wfId;
		const src = clipSource[selectedEntry.scene.id];
		if (src) out.clip_source = src;
		const draft = selectedEntry.clip.video_settings?.prompt_draft;
		const meta = selectedEntry.clip.video_settings?.prompt_draft_meta;
		if (draft) {
			out.prompt_draft = draft;
			if (meta) out.prompt_draft_meta = meta;
		}
		return out;
	}

	function settingsHash(obj: Record<string, unknown>): string {
		return JSON.stringify(obj);
	}

	$effect(() => {
		// Track the pieces that make up the persisted settings.
		const _unused = [formValues, selectedWorkflow, clipSource, selectedEntry?.clip.id];
		void _unused;
		if (!selectedEntry || saveTimer) return;
		saveTimer = setTimeout(() => {
			saveTimer = null;
			if (!selectedEntry) return;
			const next = currentClipSettings();
			const hash = settingsHash(next);
			if (hash === lastSavedHash) return;
			lastSavedHash = hash;
			projects
				.updateClip(projectId, selectedEntry.clip.id, { video_settings: next })
				.catch(() => {
					/* transient — next change retries */
				});
		}, 800);
	});

	async function onFormChangePersist() {
		// formCache write-through happens in the switch effect; autosave effect
		// handles persistence. Kept as an explicit hook for future callers.
	}

	// Context-aware defaults for a freshly opened clip form (user edits and the
	// workflow's static defaults must not override these). Duration-role inputs
	// seed from the clip's duration (falling back to the scene's estimate);
	// ComfyDynamicForm's own prefill only applies to fields still undefined.
	function seedClipDefaults(entry: { clip: Clip; scene: Scene }): Record<string, string | number> {
		const seed: Record<string, string | number> = {};
		if (!entry) return seed;
		const wf = workflowFor(entry.scene);
		const secs = entry.clip.duration_sec ?? entry.scene.duration_sec;
		for (const inp of wf?.input_schema ?? []) {
			if (normalizeInputRole(inp.role ?? null) === 'duration' && secs != null) {
				seed[inp.nodeId] = secs;
			}
		}
		return seed;
	}

	function workflowHasVideoInput(wf: Workflow | undefined): boolean {
		return Boolean(
			wf?.input_schema?.some((inp) => normalizeInputRole(inp.role ?? null) === 'video'),
		);
	}

	function videoInputNodeFor(wf: Workflow | undefined): ComfyDynamicInput | undefined {
		return wf?.input_schema?.find((inp) => normalizeInputRole(inp.role ?? null) === 'video');
	}

	// Timeline source options for a continue clip: any other rendered clip
	// (clips first, then legacy scene-level paths), ordered by timeline position.
	function timelineClipOptions(
		current: Scene | null,
	): Array<{ label: string; path: string }> {
		if (!current) return [];
		const out: Array<{ label: string; path: string }> = [];
		for (const s of scenes) {
			if (s.id === current.id) continue;
			for (const [i, c] of (s.clips ?? []).entries()) {
				if (!c.clip_path) continue;
				out.push({
					label: `${s.clips.length > 1 ? `#${s.order_index}.${i + 1}` : `#${s.order_index}`} · ${s.heading || t('queue.sceneLabel')}`,
					path: c.clip_path,
				});
			}
			if ((s.clips?.length ?? 0) === 0 && s.video_path) {
				out.push({ label: `#${s.order_index} · ${s.heading || t('queue.sceneLabel')}`, path: s.video_path });
			}
		}
		return out;
	}

const generateOne = createMutation({
		mutationFn: (vars: { clipId: number; sceneId: number; prompt?: string }) => {
			const { clipId, sceneId, prompt } = vars;
			const scene = scenes.find((s) => s.id === sceneId);
			const wf = scene ? workflowFor(scene) : undefined;
			return jobsApi.generateVideos(projectId, {
				clip_ids: [clipId],
				workflow_id: selectedWorkflow[sceneId] ?? wf?.id,
				input_values: compactInputValues(formValues),
				prompts: prompt ? { [String(clipId)]: prompt } : undefined,
			});
		},
		onSuccess: async () => {
			await client.invalidateQueries({ queryKey: ['jobs'] });
			await client.invalidateQueries({ queryKey: ['scenes'] });
			toast.success(t('queue.clipQueued'));
		},
		onError: (err) => toast.error(err instanceof Error ? err.message : String(err)),
	});

	// Render-history versioning: make an older job's output the clip's render.
	let applyingJob = $state(false);
	async function applyJobToClip(j: Job, path: string) {
		if (applyingJob) return;
		const clipId = clipIdOfJob(j) ?? selectedEntry?.clip.id;
		if (clipId == null) return;
		applyingJob = true;
		try {
			await projects.updateClip(projectId, clipId, { clip_path: path });
			await client.invalidateQueries({ queryKey: ['scenes'] });
			toast.success(t('queue.clipRenderUpdated'));
		} catch (err) {
			toast.error(err instanceof Error ? err.message : String(err));
		} finally {
			applyingJob = false;
		}
	}

	// --- HITL review gate (issue #27) ---
	let previewOpen = $state(false);

	function beginGenerate() {
		if (!selClip) return;
		previewOpen = true;
	}

	function onGenerateConfirmed(prompt: string) {
		if (!selClip || !selected) return;
		$generateOne.mutate({ clipId: selClip.id, sceneId: selected.id, prompt });
	}

	// --- Batch generate: queue clips one by one, in timeline order. ---
	// One POST per scene (the H3 prompt rewrite runs synchronously inside each
	// request, so a single all-scenes POST could take minutes and time out).
	// The queue worker then renders them strictly in sequence (concurrency 1).
	// No per-scene form values here: continue scenes resolve their previous
	// clip at enqueue or run time on the backend.
	let batching = $state(false);
	let batchNote = $state('');

	const scenesNeedingClip = $derived(
		scenes.filter((s) => !['done', 'partial', 'pending', 'running'].includes(sceneStatus(s))),
	);
	const batchTargets = $derived(
		scenesNeedingClip.length > 0
			? scenesNeedingClip
			: scenes.filter((s) => !['pending', 'running'].includes(sceneStatus(s))),
	);
	const clipTotal = $derived(
		scenes.reduce((n, s) => n + Math.max(s.clips?.length ?? 1, 1), 0),
	);
	const clipsReady = $derived(
		scenes.reduce(
			(n, s) =>
				n +
				((s.clips?.length ?? 0) > 0
					? s.clips.filter((c) => c.clip_path).length
					: s.video_path
						? 1
						: 0),
			0,
		),
	);
	const batchLabel = $derived(
		batching
			? batchNote
			: scenesNeedingClip.length > 0
				? t('queue.generateMissing', { count: clipTotal - clipsReady })
				: t('queue.regenerateAll'),
	);

	async function generateAll() {
		if (batching || batchTargets.length === 0) return;
		batching = true;
		let queued = 0;
		let drafted = 0;
		const targets = [...batchTargets].sort((a, b) => a.order_index - b.order_index);
		const totalClips = targets.reduce((n, s) => n + Math.max(s.clips?.length ?? 1, 1), 0);
		let done = 0;
		for (const scene of targets) {
			const clips = scene.clips ?? [];
			const renderTargets: Array<{ id: number; key: string }> =
				clips.length > 0
					? clips.map((c) => ({ id: c.id, key: String(c.id) }))
					: [{ id: scene.id, key: String(scene.id) }];
			for (const target of renderTargets) {
				done++;
				batchNote = t('queue.queueingProgress', { n: done, total: totalClips });
				try {
					// Resolve like the per-clip button does: session pick → scene's stored
					// workflow → first enabled video workflow. A scene whose stored workflow
					// was deleted would otherwise enqueue a job doomed to "No workflow found".
					// Fresh per-clip drafts ride along; stale/absent drafts get the backend's
					// auto-rewrite (deterministic template on LLM failure).
					const clip = clips.length > 0 ? clips.find((c) => c.id === target.id) : null;
					const draft = clip?.video_settings?.prompt_draft;
					const draftFresh =
						draft && clip?.video_settings?.prompt_draft_meta?.based_on
							? clip.video_settings.prompt_draft_meta.based_on
							: null;
					await jobsApi.generateVideos(projectId, {
						clip_ids: clip ? [target.id] : undefined,
						scene_ids: clip ? undefined : [target.id],
						workflow_id: workflowFor(scene)?.id,
						prompts: draft ? { [target.key]: draft } : undefined,
					});
					queued++;
					if (draft) drafted++;
					client.invalidateQueries({ queryKey: ['jobs'] });
					client.invalidateQueries({ queryKey: ['scenes'] });
				} catch (err) {
					const label =
						clips.length > 0
							? t('queue.clipRef', { ref: `#${scene.order_index}.${(clips.findIndex((c) => c.id === target.id) ?? 0) + 1}` })
							: t('queue.sceneRef', { ref: `#${scene.order_index}` });
					toast.error(`${label}: ${err instanceof Error ? err.message : String(err)}`);
				}
			}
		}
		batching = false;
		batchNote = '';
		if (queued > 0) {
			const draftNote =
				drafted > 0 ? ` · ${t('queue.savedDrafts', { count: drafted })}` : '';
			toast.success(t('queue.queuedInSequence', { count: queued, draftNote }));
		}
		await client.invalidateQueries({ queryKey: ['jobs'] });
		await client.invalidateQueries({ queryKey: ['scenes'] });
	}

	function workflowFor(scene: Scene): Workflow | undefined {
		const id = selectedWorkflow[scene.id] ?? scene.workflow_id ?? undefined;
		return enabledWorkflows.find((w) => w.id === id) ?? enabledWorkflows[0] ?? undefined;
	}

	// Clip-aware helpers: jobs are clip-addressed (job.clip_id); legacy rows
	// without clip_id fall back to scene addressing (1:1 projects keep working).
	function clipIdOfJob(j: Job): number | null {
		return j.clip_id ?? null;
	}

	function jobForClipId(clipId: number): Job | undefined {
		const jobs = ($jobsQuery.data ?? []).filter((j) => clipIdOfJob(j) === clipId && j.kind === 'video');
		if (jobs.length === 0) return undefined;
		return [...jobs].sort((a, b) => b.id - a.id)[0];
	}

	/** Latest video job for a scene (legacy fallback + history views). */
	function jobForScene(sceneId: number): Job | undefined {
		const jobs = ($jobsQuery.data ?? []).filter((j) => j.scene_id === sceneId && j.kind === 'video');
		if (jobs.length === 0) return undefined;
		return [...jobs].sort((a, b) => b.id - a.id)[0];
	}

	/** Any pending/running video job inside this scene (any of its clips). */
	function sceneBusy(scene: Scene): boolean {
		return ($jobsQuery.data ?? []).some(
			(j) =>
				j.kind === 'video' &&
				j.scene_id === scene.id &&
				(j.status === 'pending' || j.status === 'running'),
		);
	}

	function sceneStatus(scene: Scene): string {
		if (sceneBusy(scene)) return 'running';
		const clips = scene.clips ?? [];
		if (clips.length > 0) {
			if (clips.every((c) => c.clip_path)) return 'done';
			if (clips.some((c) => c.clip_path)) return 'partial';
			return 'idle';
		}
		const job = jobForScene(scene.id);
		if (job) return job.status;
		if (scene.video_path) return 'done';
		return 'idle';
	}

	function clipBusy(clipId: number): boolean {
		const job = jobForClipId(clipId);
		return job?.status === 'pending' || job?.status === 'running';
	}

	function statusOfClip(clipId: number): string {
		if (clipBusy(clipId)) return 'running';
		const entry = filmClips.find((c) => c.clip.id === clipId);
		if (!entry) return 'idle';
		if (entry.clip.clip_path) return 'done';
		const job = jobForClipId(clipId);
		if (job) return job.status;
		// Un-expanded/legacy clip mirrors its scene row.
		if (entry.index === 0 && entry.scene.video_path && (entry.scene.clips?.length ?? 0) <= 1) {
			return 'done';
		}
		return 'idle';
	}

	/** Playback label: '#3' for a scene's only clip, '#3.2' for shot 2 of 3+. */
	function clipLabel(scene: Scene, index: number): string {
		return (scene.clips?.length ?? 0) > 1
			? `#${scene.order_index}.${index + 1}`
			: `#${scene.order_index}`;
	}

	type Thumb = { kind: 'image' | 'video'; src: string } | null;

	function thumbForClip(clipId: number): Thumb | null {
		const entry = filmClips.find((c) => c.clip.id === clipId);
		if (!entry) return null;
		if (entry.clip.clip_path && /\.(mp4|webm)$/i.test(entry.clip.clip_path)) {
			const src = assetUrl(entry.clip.clip_path);
			if (src) return { kind: 'video', src };
		}
		if (entry.index === 0 && entry.scene.env_image_path) {
			const src = assetUrl(entry.scene.env_image_path);
			if (src) return { kind: 'image', src };
		}
		return null;
	}

	function previewPathForClip(clipId: number): string | null {
		const entry = filmClips.find((c) => c.clip.id === clipId);
		if (!entry) return null;
		if (clipBusy(clipId)) return null;
		const path = entry.clip.clip_path;
		if (path && /\.(mp4|webm)$/i.test(path)) return path;
		// Legacy un-expanded clip: the scene row mirrors the render.
		if (
			entry.index === 0 &&
			(entry.scene.clips?.length ?? 0) <= 1 &&
			entry.scene.video_path &&
			/\.(mp4|webm)$/i.test(entry.scene.video_path)
		) {
			return entry.scene.video_path;
		}
		return null;
	}

	function formatClock(sec: number): string {
		const s = Math.max(0, Math.round(sec));
		const m = Math.floor(s / 60);
		const r = s % 60;
		return `${m}:${r.toString().padStart(2, '0')}`;
	}

	// Same compact "3m ago" style as ProjectCard — local copy keeps this stage self-contained.
	function relativeTime(iso: string): string {
		const then = new Date(iso).getTime();
		if (Number.isNaN(then)) return '';
		const mins = Math.max(0, Math.round((Date.now() - then) / 60000));
		if (mins < 1) return 'just now';
		if (mins < 60) return `${mins}m ago`;
		const hours = Math.round(mins / 60);
		if (hours < 24) return `${hours}h ago`;
		const days = Math.round(hours / 24);
		if (days < 30) return `${days}d ago`;
		const months = Math.round(days / 30);
		if (months < 12) return `${months}mo ago`;
		return `${Math.round(months / 12)}y ago`;
	}

	const doneCount = $derived(clipsReady);

	async function togglePause() {
		if ($queueStatusQuery.data?.paused) await jobsApi.resume();
		else await jobsApi.pause();
		client.invalidateQueries({ queryKey: ['queue-status'] });
	}

	async function resumeQueue() {
		await jobsApi.resume();
		client.invalidateQueries({ queryKey: ['queue-status'] });
	}

	/** Select a clip; the parent scene follows for the script drawer + form. */
	function selectClip(id: number) {
		selectedClipId = id;
	}

	// Filmstrip click → jump back to the editor with that clip selected.
	function editScene(id: number) {
		const entry = allClips.find((c) => c.scene.id === id);
		if (entry) {
			selectedClipId = entry.clip.id;
			view = 'edit';
		}
	}

	function neighborClipId(dir: -1 | 1): number | null {
		const idx = allClips.findIndex((c) => c.clip.id === selectedClipId);
		const next = allClips[(idx < 0 ? 0 : idx) + dir];
		return next?.clip.id ?? null;
	}

	function step(dir: -1 | 1) {
		const id = neighborClipId(dir);
		if (id != null) selectedClipId = id;
	}

	const selClip = $derived(selectedEntry?.clip ?? null);
	const selJob = $derived(selClip ? jobForClipId(selClip.id) : undefined);
	const selProg = $derived(selJob ? progressFor(selJob.id) : undefined);
	const selError = $derived(selJob?.error ?? '');
	const selErrorLong = $derived(selError.length > 140 || selError.split('\n').length > 3);

	// --- Export film (latest export job drives the Film view; no extra polling) ---
	const exportJob = $derived(
		($jobsQuery.data ?? [])
			.filter((j) => j.kind === 'export')
			.sort((a, b) => b.id - a.id)[0] ?? null,
	);
	const exportActive = $derived(
		exportJob?.status === 'pending' || exportJob?.status === 'running',
	);
	const exportProg = $derived(exportActive && exportJob ? progressFor(exportJob.id) : undefined);
	const exportPath = $derived(
		exportJob?.status === 'done'
			? ((exportJob.output_paths ?? []).find((p) => /\.mp4$/i.test(p)) ?? null)
			: null,
	);
	// A clip finished after the export completed → the film no longer matches the timeline.
	const exportStale = $derived.by(() => {
		if (exportJob?.status !== 'done' || !exportJob.completed_at) return false;
		const exportAt = Date.parse(exportJob.completed_at);
		if (Number.isNaN(exportAt)) return false;
		return ($jobsQuery.data ?? []).some(
			(j) =>
				j.kind === 'video' &&
				j.status === 'done' &&
				j.completed_at != null &&
				Date.parse(j.completed_at) > exportAt,
		);
	});
	const clipsMissing = $derived(clipTotal - clipsReady);

	type ExportState = 'idle' | 'active' | 'ready' | 'failed';
	const exportState = $derived.by((): ExportState => {
		if (!exportJob) return 'idle';
		if (exportActive) return 'active';
		if (exportJob.status === 'done') return 'ready';
		if (exportJob.status === 'failed') return 'failed';
		return 'idle';
	});

	// Clip count recorded on the export job payload when the backend provides it;
	// otherwise fall back to the clips currently on the timeline.
	const exportClipCount = $derived.by(() => {
		const p = exportJob?.payload;
		const n = p?.clips ?? p?.clip_count;
		return typeof n === 'number' && Number.isFinite(n) && n > 0 ? n : clipsReady;
	});
	const exportedAgo = $derived(
		exportJob?.completed_at ? relativeTime(exportJob.completed_at) : '',
	);

	const filmChip = $derived.by((): { status: string; label: string } => {
		if (exportState === 'active') return { status: 'running', label: t('queue.exporting') };
		if (exportState === 'ready') {
			return exportStale
				? { status: 'paused', label: t('queue.clipsChanged') }
				: { status: 'ready', label: t('common.ready') };
		}
		if (exportState === 'failed') return { status: 'failed', label: t('queue.exportFailed') };
		return { status: 'idle', label: t('queue.notExported') };
	});

	// Small status dot on the Film tab; the Film view itself carries the full text state.
	const filmDot = $derived.by((): 'success' | 'info' | 'warning' | null => {
		if (exportState === 'active') return 'info';
		if (exportState === 'ready') return exportStale ? 'warning' : 'success';
		return null;
	});

	const exportFilm = createMutation({
		mutationFn: () => jobsApi.exportFilm(projectId),
		onSuccess: async () => {
			await client.invalidateQueries({ queryKey: ['jobs'] });
			toast.success(t('queue.exportQueued'));
		},
		onError: (err) => toast.error(err instanceof Error ? err.message : String(err)),
	});

	async function cancelExport() {
		if (!exportJob) return;
		try {
			await jobsApi.cancel(exportJob.id);
			await client.invalidateQueries({ queryKey: ['jobs'] });
		} catch (err) {
			toast.error(err instanceof Error ? err.message : String(err));
		}
	}
</script>

<div class="queue-root" class:is-edit={view === 'edit'}>
<header class="stage-header">
	<div>
		<h2>4. {t('nav.video')}</h2>
		<p class="muted">
			{#if scenes.length === 0}
				{t('queue.emptyHeader')}
			{:else}
				{t('queue.clipsDoneSummary', {
					done: doneCount,
					total: clipTotal,
					time: formatClock(totalSec),
					scenes: scenes.length,
				})}
			{/if}
		</p>
	</div>
	<div class="actions">
		{#if scenes.length > 0}
			<Button
				variant="primary"
				disabled={batching || batchTargets.length === 0}
				loading={batching}
				title={t('queue.generateAllTitle')}
				onclick={generateAll}
			>
				<Icon name="film" size={14} /> {batchLabel}
			</Button>
		{/if}
		<Button variant="secondary" onclick={togglePause}>
			{$queueStatusQuery.data?.paused ? t('queue.resumeQueue') : t('queue.pauseQueue')}
		</Button>
		<div class="view-toggle" role="tablist" aria-label={t('queue.viewAria')}>
			<button
				type="button"
				role="tab"
				aria-selected={view === 'edit'}
				class="view-tab"
				class:active={view === 'edit'}
				onclick={() => (view = 'edit')}
			>
				<Icon name="edit" size={14} /> {t('queue.editTab')}
			</button>
			<button
				type="button"
				role="tab"
				aria-selected={view === 'film'}
				class="view-tab"
				class:active={view === 'film'}
				onclick={() => (view = 'film')}
			>
				<Icon name="film" size={14} /> {t('queue.filmTab')}
				{#if filmDot}
					<span class="film-dot dot-{filmDot}" aria-hidden="true"></span>
				{/if}
			</button>
		</div>
	</div>
</header>

{#if view === 'edit'}
	{#if $queueStatusQuery.data?.paused}
		<div class="paused-banner" role="status">
			<Icon name="alert" size={16} />
			<StatusChip status="paused" label={t('queue.paused')} />
			<span class="paused-text">{t('queue.pausedText')}</span>
			<span class="paused-action">
				<Button size="sm" variant="secondary" onclick={resumeQueue}
					>{t('queue.resumeNow')}</Button
				>
			</span>
		</div>
	{/if}

	{#if scenes.length === 0}
		<div class="empty">
			<p class="empty-title">{t('queue.noTimeline')}</p>
			<p class="muted">{t('queue.noTimelineBody')}</p>
		</div>
	{:else if selected}
		{@const selWf = workflowFor(selected)}
		{@const selStatus = selClip ? statusOfClip(selClip.id) : sceneStatus(selected)}
		{@const selPreview = selClip ? previewPathForClip(selClip.id) : null}
		{@const selHasVideoInput = workflowHasVideoInput(selWf)}
		{@const selVideoNode = videoInputNodeFor(selWf)}
		{@const selClips = timelineClipOptions(selected)}
		{@const selChain = Boolean(selClip?.chain_from_prev ?? selected.chain_from_prev)}
		{@const selSource = clipSource[selected.id] ?? 'auto'}
		{@const selSourceValid = selSource === 'auto' || selSource === 'upload' || selClips.some((o) => o.path === selSource)}
		{@const selBlocked = selChain && !selHasVideoInput}
		<input
			bind:this={videoFileInput}
			type="file"
			class="sr-only-video-file"
			accept="video/*,.mp4,.webm,.mov,.mkv"
			onchange={onVideoFileChosen}
		/>
		<VideoEditWorkspace
			{scenes}
			{filmClips}
			selectedClip={selectedEntry}
			{selectedClipId}
			{selected}
			status={selStatus}
			previewPath={selPreview}
			progress={selProg}
			error={selError}
			errorLong={selErrorLong}
			job={selJob}
			clipJobs={($jobsQuery.data ?? []).filter(
				(j) => selClip != null && clipIdOfJob(j) === selClip.id && j.kind === 'video',
			)}
			workflow={selWf}
			workflows={enabledWorkflows}
			bind:formValues
			{assetOptions}
			allowUpload
			submitting={$generateOne.isPending}
			statusOfClip={statusOfClip}
			thumbForClip={thumbForClip}
			{formatClock}
			onApplyToClip={(j, path) => applyJobToClip(j, path)}
			applying={applyingJob}
			generateDisabled={selBlocked}
			generateDisabledReason={selBlocked
				? t('queue.chainDisabledReason')
				: ''}
			clipSource={{
				enabled: selChain && selHasVideoInput && Boolean(selVideoNode),
				value: selSourceValid ? selSource : 'auto',
				options: selClips.map((o) => ({
					id: o.path,
					label: o.label,
					path: o.path,
				})),
			}}
			onClipSourceChange={(val) => {
				if (val === 'auto') {
					// Back to Auto: send nothing — the backend resolves the previous clip.
					const next = { ...clipSource };
					delete next[selected.id];
					clipSource = next;
					if (selVideoNode) formValues = { ...formValues, [selVideoNode.nodeId]: '' };
				} else if (val === 'upload') {
					clipSource = { ...clipSource, [selected.id]: 'upload' };
				} else {
					clipSource = { ...clipSource, [selected.id]: val };
					if (selVideoNode) {
						formValues = { ...formValues, [selVideoNode.nodeId]: val };
					}
				}
			}}
			onClipSourceUpload={() => videoFileInput?.click()}
			onSelectClip={selectClip}
			onStep={step}
			onWorkflowChange={(id) => {
				selectedWorkflow = { ...selectedWorkflow, [selected.id]: Number(id) };
			}}
		onGenerate={() => {
			if (selClip) $generateOne.mutate({ clipId: selClip.id, sceneId: selected.id });
		}}
		onPreviewPrompt={beginGenerate}
	/>

	<PromptPreviewModal
		bind:open={previewOpen}
		{projectId}
		clip={selClip}
		scene={selected}
		workflow={selWf}
		inputValues={formValues}
		onConfirm={onGenerateConfirmed}
	/>
	{/if}
{:else}
	<section class="film-view" aria-label={t('queue.filmViewAria')}>
		<div class="marquee">
			<span class="marquee-icon" aria-hidden="true"><Icon name="film" size={20} /></span>
			<h2 class="marquee-title">{projectTitle}</h2>
			<span class="marquee-chip">
				<StatusChip status={filmChip.status} label={filmChip.label} />
			</span>
		</div>

		<div class="program-frame">
{#if exportState === 'ready'}
			<SafeMedia
				class="program-media"
				src={assetUrl(exportPath)}
				kind="video"
				label={t('queue.exportUnavailable')}
			/>
			{#if exportStale}
				<div class="stale-banner" role="status">
					<Icon name="alert" size={14} />
					<span>{t('queue.staleBanner')}</span>
				</div>
			{/if}
		{:else if exportState === 'active'}
			<div class="program-slate">
				<Spinner size="lg" />
				<h3 class="slate-title">{t('queue.exportingTitle')}</h3>
				<div class="slate-progress">
					<ProgressBar
						value={exportProg?.progress ?? 0}
						indeterminate={exportProg == null}
						label={exportProg?.message}
					/>
				</div>
				<p class="slate-sub">{t('queue.exportingSub', { n: clipsReady })}</p>
				<Button variant="ghost" size="sm" onclick={cancelExport}
					>{t('common.cancel')}</Button
				>
			</div>
		{:else if exportState === 'failed'}
			<div class="program-slate">
				<span class="slate-icon slate-icon-err" aria-hidden="true">
					<Icon name="alert" size={32} />
				</span>
				<h3 class="slate-title">{t('queue.exportFailed')}</h3>
				<p class="slate-err" title={exportJob?.error ?? t('queue.exportFailed')}>
					{exportJob?.error || t('queue.exportFailed')}
				</p>
				<Button
					variant="secondary"
					loading={$exportFilm.isPending}
					onclick={() => $exportFilm.mutate()}
				>
					{t('queue.retryExport')}
				</Button>
			</div>
		{:else}
			<div class="program-slate">
				<span class="slate-icon" aria-hidden="true">
					<Icon name="film" size={32} />
				</span>
				<h3 class="slate-title">{t('queue.notExportedTitle')}</h3>
				<p class="slate-sub">
					{t('queue.slateSub', {
						clips: clipsReady,
						time: formatClock(totalSec),
					})}
				</p>
				{#if clipsMissing > 0}
					<p class="slate-warn" role="status">
						<Icon name="alert" size={14} />
						<span>{t('queue.clipsMissing', { count: clipsMissing })}</span>
					</p>
				{/if}
				<Button
					variant="primary"
					disabled={clipsReady === 0}
					loading={$exportFilm.isPending}
					title={
						clipsReady === 0
							? t('queue.exportDisabledTitle')
							: t('queue.exportFilm')
					}
					onclick={() => $exportFilm.mutate()}
				>
					<Icon name="film" size={14} /> {t('queue.exportFilm')}
				</Button>
			</div>
		{/if}
	</div>

	{#if exportState === 'ready'}
		{@const exportUrl = assetUrl(exportPath)}
		<div class="film-meta">
			<span class="film-meta-text">
				{t('queue.filmMeta', { scenes: scenes.length, clips: exportClipCount, duration: formatClock(totalSec) })}
				{#if exportedAgo} · {t('queue.exportedAgo', { ago: exportedAgo })}{/if}
			</span>
			<div class="film-actions">
				{#if exportUrl}
					<a class="btn-dl" href={exportUrl} download={`${projectTitle}.mp4`}>
						<Icon name="download" size={14} /> {t('queue.downloadFilm')}
					</a>
				{/if}
				<Button
					variant={exportStale ? 'primary' : 'ghost'}
					size="sm"
					loading={$exportFilm.isPending}
					onclick={() => $exportFilm.mutate()}
				>
					{t('queue.reexport')}
				</Button>
			</div>
		</div>
	{/if}

	{#if filmClips.length > 0}
		<div class="filmstrip-block">
			<p class="filmstrip-label">{t('queue.inThisFilm')}</p>
			<div class="filmstrip">
				{#each filmClips as entry (entry.clip.id)}
					{@const thumb = thumbForClip(entry.clip.id)}
					<button
						type="button"
						class="filmstrip-item"
						title={t('queue.editInTimeline', {
							scene: entry.scene.heading || t('queue.scene', { n: entry.scene.order_index }),
							label: entry.label,
						})}
						onclick={() => editScene(entry.scene.id)}
					>
							{#if thumb?.kind === 'image'}
								<img class="filmstrip-media" src={thumb.src} alt="" loading="lazy" />
							{:else if thumb?.kind === 'video'}
								<!-- svelte-ignore a11y_media_has_caption -->
								<video class="filmstrip-media" src={thumb.src} muted playsinline preload="metadata"></video>
							{:else}
								<span class="filmstrip-slate">{entry.label}</span>
							{/if}
							<span class="filmstrip-num">{entry.label}</span>
						</button>
					{/each}
				</div>
			</div>
		{/if}
	</section>
{/if}
</div>

<style>
	.sr-only-video-file {
		position: absolute;
		width: 1px;
		height: 1px;
		padding: 0;
		margin: -1px;
		overflow: hidden;
		clip: rect(0 0 0 0);
		white-space: nowrap;
		border: 0;
	}

	.queue-root {
		display: flex;
		flex-direction: column;
		height: 100%;
		min-height: 0;
		gap: 10px;
		overflow-y: auto;
	}
	.queue-root.is-edit {
		overflow: hidden;
	}

	.stage-header {
		display: flex;
		justify-content: space-between;
		align-items: flex-start;
		gap: var(--space-md);
		flex-shrink: 0;
	}
	.stage-header h2 {
		margin: 0 0 4px;
		font-size: 20px;
		font-weight: 700;
	}
	.muted {
		color: var(--text-secondary);
		margin: 0;
	}
	.small {
		font-size: 12px;
		line-height: 1.4;
	}
	.actions {
		display: flex;
		gap: 10px;
		align-items: center;
		flex-wrap: wrap;
		justify-content: flex-end;
	}
	.view-toggle {
		display: inline-flex;
		padding: 3px;
		gap: 2px;
		background: var(--bg-elevated);
		border: 1px solid var(--border);
		border-radius: 9999px;
	}
	.view-tab {
		display: inline-flex;
		align-items: center;
		gap: 6px;
		height: 30px;
		padding: 0 12px;
		border: none;
		border-radius: 9999px;
		background: transparent;
		color: var(--text-secondary);
		font-size: 12px;
		font-weight: 600;
		font-family: var(--font-body);
		cursor: pointer;
	}
	.view-tab:hover {
		color: var(--text-primary);
	}
	.view-tab.active {
		background: var(--bg-surface);
		color: var(--text-primary);
		box-shadow: 0 0 0 1px var(--accent);
	}
	.film-dot {
		width: 7px;
		height: 7px;
		border-radius: 9999px;
	}
	.film-dot.dot-success {
		background: var(--success);
	}
	.film-dot.dot-info {
		background: var(--warning);
	}
	.film-dot.dot-warning {
		background: var(--warning);
	}

	.paused-banner {
		display: flex;
		align-items: center;
		gap: 10px;
		padding: 10px 12px;
		border-radius: var(--radius-md);
		border: 1px solid color-mix(in srgb, var(--warning) 40%, var(--border));
		background: color-mix(in srgb, var(--warning) 10%, var(--bg-surface));
		flex-shrink: 0;
	}
	.paused-text {
		flex: 1;
		font-size: 13px;
		color: var(--text-secondary);
	}
	.paused-action {
		flex-shrink: 0;
	}

	.empty {
		padding: 48px 24px;
		text-align: center;
		border: 1px dashed var(--border);
		border-radius: var(--radius-md);
		background: var(--bg-surface);
		margin: auto 0;
	}
	.empty-title {
		margin: 0 0 8px;
		font-weight: 650;
	}


	/* --- Film view (screening room) --- */
	.film-view {
		max-width: 1080px;
		margin: 0 auto;
		padding: var(--space-lg) var(--space-md) var(--space-xl);
		display: flex;
		flex-direction: column;
		gap: var(--space-md);
	}
	.marquee {
		display: flex;
		align-items: center;
		gap: 10px;
	}
	.marquee-icon {
		display: flex;
		align-items: center;
		color: var(--accent);
		flex-shrink: 0;
	}
	.marquee-title {
		margin: 0;
		font-size: 22px;
		font-weight: 700;
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.marquee-chip {
		margin-left: auto;
		flex-shrink: 0;
		display: flex;
		align-items: center;
	}
	.program-frame {
		position: relative;
		width: 100%;
		aspect-ratio: 16 / 9;
		background: #000;
		border: 1px solid var(--border);
		border-radius: var(--radius-md);
		overflow: hidden;
	}
	.program-frame :global(.program-media) {
		width: 100%;
		height: 100%;
		object-fit: contain;
		background: #000;
		border: none;
		border-radius: 0;
		min-height: 0;
	}
	.stale-banner {
		position: absolute;
		inset: 0 0 auto 0;
		z-index: 1;
		display: flex;
		align-items: center;
		justify-content: center;
		gap: 8px;
		padding: 8px 12px;
		background: rgba(245, 158, 11, 0.12);
		border-bottom: 1px solid rgba(245, 158, 11, 0.35);
		color: var(--warning);
		font-size: 13px;
	}
	.program-slate {
		position: absolute;
		inset: 0;
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		gap: 12px;
		padding: var(--space-lg);
		text-align: center;
		overflow-y: auto;
	}
	.slate-icon {
		display: flex;
		color: var(--text-muted);
	}
	.slate-icon-err {
		color: var(--error);
	}
	.slate-title {
		margin: 0;
		font-size: 20px;
		font-weight: 650;
	}
	.slate-sub {
		margin: 0;
		font-size: 13px;
		color: var(--text-secondary);
	}
	.slate-progress {
		width: 100%;
		max-width: 420px;
	}
	.slate-warn {
		margin: 0;
		display: flex;
		align-items: center;
		gap: 6px;
		font-size: 13px;
		color: var(--warning);
	}
	.slate-err {
		margin: 0;
		max-width: 60ch;
		font-size: 13px;
		color: var(--error);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.film-meta {
		display: flex;
		align-items: center;
		justify-content: space-between;
		flex-wrap: wrap;
		gap: var(--space-sm);
	}
	.film-meta-text {
		font-family: var(--font-mono);
		font-size: 12px;
		color: var(--text-muted);
	}
	.film-actions {
		display: flex;
		align-items: center;
		gap: 8px;
		flex-shrink: 0;
	}
	.btn-dl {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		gap: 8px;
		min-height: 28px;
		padding: 5px 10px;
		font-size: 12px;
		font-weight: 500;
		font-family: inherit;
		white-space: nowrap;
		background: var(--bg-elevated);
		border: 1px solid var(--border);
		border-radius: var(--radius-sm);
		color: var(--text-primary);
		text-decoration: none;
		cursor: pointer;
		transition:
			background-color 150ms ease,
			border-color 150ms ease;
	}
	.btn-dl:hover {
		background: #23232b;
		border-color: #3f3f46;
	}
	.btn-dl:focus-visible {
		outline: 2px solid var(--accent);
		outline-offset: 2px;
	}
	.filmstrip-block {
		display: flex;
		flex-direction: column;
		gap: 8px;
	}
	.filmstrip-label {
		margin: 0;
		font-family: var(--font-mono);
		font-size: 11px;
		letter-spacing: 0.08em;
		text-transform: uppercase;
		color: var(--text-muted);
	}
	.filmstrip {
		display: flex;
		gap: 6px;
		overflow-x: auto;
		padding-bottom: 4px;
	}
	.filmstrip-item {
		position: relative;
		flex-shrink: 0;
		width: 104px;
		height: 64px;
		padding: 0;
		border: 1px solid var(--border);
		border-radius: var(--radius-sm);
		background: var(--bg-elevated);
		overflow: hidden;
		cursor: pointer;
		transition: border-color 150ms ease;
	}
	.filmstrip-item:hover {
		border-color: #52525b;
	}
	.filmstrip-item:focus-visible {
		outline: 2px solid var(--accent);
		outline-offset: 2px;
	}
	.filmstrip-media {
		display: block;
		width: 100%;
		height: 100%;
		object-fit: cover;
		pointer-events: none;
	}
	.filmstrip-slate {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 100%;
		height: 100%;
		font-family: var(--font-mono);
		font-size: 13px;
		font-weight: 700;
		color: var(--text-muted);
	}
	.filmstrip-num {
		position: absolute;
		top: 4px;
		left: 4px;
		padding: 1px 5px;
		border-radius: 4px;
		background: rgba(0, 0, 0, 0.65);
		font-family: var(--font-mono);
		font-size: 10px;
		font-weight: 700;
		color: var(--text-primary);
	}
</style>
