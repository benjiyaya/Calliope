<script lang="ts">
	import { createMutation, createQuery, useQueryClient } from '@tanstack/svelte-query';
	import { toStore } from 'svelte/store';
	import { toast } from '$lib/toast';
	import {
		assetUrl,
		jobsApi,
		playgroundApi,
		projects,
		workflows,
		type Job,
		type Scene,
		type Workflow,
	} from '$lib/api';
	import { compactInputValues } from '$lib/comfy/promptInput';
	import { createUploadManager } from '$lib/comfy/useUpload.svelte';
	import { normalizeInputRole } from '$lib/comfy/parser';
	import type { ComfyDynamicInput } from '$lib/comfy/types';
	import type { AssetOption } from '$lib/assetPicker';
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
	let selectedId = $state<number | null>(null);
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
			refetchInterval: 5000,
		})),
	);
	const queueStatusQuery = createQuery({
		queryKey: ['queue-status'],
		queryFn: jobsApi.queueStatus,
		refetchInterval: 5000,
	});
	const uploadsQuery = createQuery({
		queryKey: ['playground-uploads'],
		queryFn: playgroundApi.listUploads,
	});

	const scenes = $derived(($scenesQuery.data?.scenes ?? []) as Scene[]);
	const totalSec = $derived(
		scenes.reduce((sum, s) => sum + Math.max(s.duration_sec || 5, 1), 0),
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
					label: `Clip #${sc.order_index} · ${sc.heading || 'scene'}`,
					path: sc.video_path,
					kind: 'video',
					group: 'clip',
				});
			}
		}
		for (const up of $uploadsQuery.data ?? []) {
			opts.push({ label: `${up.name} · upload`, path: up.path, kind: up.kind, group: 'upload' });
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
		if (selectedId != null) return;
		if (scenes.length > 0) selectedId = scenes[0].id;
	});

	const selected = $derived(scenes.find((s) => s.id === selectedId) ?? null);

	$effect(() => {
		const id = selected?.id ?? null;
		if (id === lastFormScene) return;
		if (lastFormScene != null) formCache.set(lastFormScene, formValues);
		lastFormScene = id;
		if (id != null) {
			const stored = formCache.get(id);
			if (stored) {
				formValues = { ...stored };
			} else if (selected?.video_settings?.input_values) {
				// First open of a persisted setup: hydrate from the scene row.
				formValues = {
					...seedSceneDefaults(selected),
					...selected.video_settings.input_values,
				};
			} else {
				formValues = { ...seedSceneDefaults(selected) };
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

	function currentVideoSettings(): Record<string, unknown> {
		if (!selected) return {};
		const out: Record<string, unknown> = {
			input_values: compactInputValues(formValues),
		};
		const wfId = selectedWorkflow[selected.id];
		if (wfId) out.form_workflow_id = wfId;
		const src = clipSource[selected.id];
		if (src) out.clip_source = src;
		const draft = selected.video_settings?.prompt_draft;
		const meta = selected.video_settings?.prompt_draft_meta;
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
		const _unused = [formValues, selectedWorkflow, clipSource, selected?.id];
		void _unused;
		if (!selected || saveTimer) return;
		saveTimer = setTimeout(() => {
			saveTimer = null;
			if (!selected) return;
			const next = currentVideoSettings();
			const hash = settingsHash(next);
			if (hash === lastSavedHash) return;
			lastSavedHash = hash;
			projects
				.updateScene(projectId, selected.id, { video_settings: next })
				.catch(() => {
					/* transient — next change retries */
				});
		}, 800);
	});

	async function onFormChangePersist() {
		// formCache write-through happens in the switch effect; autosave effect
		// handles persistence. Kept as an explicit hook for future callers.
	}

	// Context-aware defaults for a freshly opened scene form (user edits and the
	// workflow's static defaults must not override these). Duration-role inputs
	// seed from the scene's estimated duration; ComfyDynamicForm's own prefill
	// only applies to fields still undefined afterwards.
	function seedSceneDefaults(scene: Scene | null): Record<string, string | number> {
		const seed: Record<string, string | number> = {};
		if (!scene) return seed;
		const wf = workflowFor(scene);
		for (const inp of wf?.input_schema ?? []) {
			if (inp.role === 'duration' && scene.duration_sec != null) {
				seed[inp.nodeId] = scene.duration_sec;
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

	// Timeline source options for a continue scene: any other scene that has
	// rendered a clip, ordered by timeline position.
	function timelineClipOptions(current: Scene | null): Scene[] {
		if (!current) return [];
		return scenes
			.filter((s) => s.id !== current.id && s.video_path)
			.sort((a, b) => a.order_index - b.order_index);
	}

	const generateOne = createMutation({
		mutationFn: (vars: { sceneId: number; prompt?: string }) => {
			const { sceneId } = vars;
			const scene = scenes.find((s) => s.id === sceneId);
			const wf = scene ? workflowFor(scene) : undefined;
			return jobsApi.generateVideos(projectId, {
				scene_ids: [sceneId],
				workflow_id: selectedWorkflow[sceneId] ?? wf?.id,
				input_values: compactInputValues(formValues),
				prompts: vars.prompt ? { [String(sceneId)]: vars.prompt } : undefined,
			});
		},
		onSuccess: async () => {
			await client.invalidateQueries({ queryKey: ['jobs'] });
			await client.invalidateQueries({ queryKey: ['scenes'] });
			toast.success('Clip queued');
		},
		onError: (err) => toast.error(err instanceof Error ? err.message : String(err)),
	});

	// --- HITL review gate (issue #27) ---
	let previewOpen = $state(false);

	function beginGenerate() {
		if (!selected) return;
		previewOpen = true;
	}

	function onGenerateConfirmed(prompt: string) {
		if (!selected) return;
		$generateOne.mutate({ sceneId: selected.id, prompt });
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
		scenes.filter((s) => !['done', 'pending', 'running'].includes(statusOf(s))),
	);
	const batchTargets = $derived(
		scenesNeedingClip.length > 0
			? scenesNeedingClip
			: scenes.filter((s) => !['pending', 'running'].includes(statusOf(s))),
	);
	const batchLabel = $derived(
		batching
			? batchNote
			: scenesNeedingClip.length > 0
				? `Generate all (${scenesNeedingClip.length})`
				: 'Regenerate all',
	);

	async function generateAll() {
		if (batching || batchTargets.length === 0) return;
		batching = true;
		let queued = 0;
		let drafted = 0;
		const targets = [...batchTargets].sort((a, b) => a.order_index - b.order_index);
		for (let i = 0; i < targets.length; i++) {
			const scene = targets[i];
			batchNote = `Queueing ${i + 1}/${targets.length}…`;
			try {
				// Resolve like the per-scene button does: session pick → scene's stored
				// workflow → first enabled video workflow. A scene whose stored workflow
				// was deleted would otherwise enqueue a job doomed to "No workflow found".
				// Saved prompt drafts ride along; un-drafted scenes get the backend's
				// auto-rewrite (deterministic template on LLM failure).
				const draft = scene.video_settings?.prompt_draft;
				const draftFresh =
					draft && scene.video_settings?.prompt_draft_meta?.based_on
						? scene.video_settings.prompt_draft_meta.based_on
						: null;
				await jobsApi.generateVideos(projectId, {
					scene_ids: [scene.id],
					workflow_id: workflowFor(scene)?.id,
					prompts: draft ? { [String(scene.id)]: draft } : undefined,
				});
				queued++;
				if (draft) drafted++;
				client.invalidateQueries({ queryKey: ['jobs'] });
				client.invalidateQueries({ queryKey: ['scenes'] });
			} catch (err) {
				toast.error(
					`Scene #${scene.order_index}: ${err instanceof Error ? err.message : String(err)}`,
				);
			}
		}
		batching = false;
		batchNote = '';
		if (queued > 0) {
			const draftNote = drafted > 0 ? ` · ${drafted} saved draft${drafted === 1 ? '' : 's'}` : '';
			toast.success(
				`${queued} clip${queued === 1 ? '' : 's'} queued — rendering in sequence${draftNote}`,
			);
		}
		await client.invalidateQueries({ queryKey: ['jobs'] });
		await client.invalidateQueries({ queryKey: ['scenes'] });
	}

	function workflowFor(scene: Scene): Workflow | undefined {
		const id = selectedWorkflow[scene.id] ?? scene.workflow_id ?? undefined;
		return enabledWorkflows.find((w) => w.id === id) ?? enabledWorkflows[0] ?? undefined;
	}

	function jobForScene(sceneId: number): Job | undefined {
		const jobs = ($jobsQuery.data ?? []).filter((j) => j.scene_id === sceneId && j.kind === 'video');
		if (jobs.length === 0) return undefined;
		return [...jobs].sort((a, b) => b.id - a.id)[0];
	}

	function statusOf(scene: Scene): string {
		const job = jobForScene(scene.id);
		if (job) return job.status;
		if (scene.video_path) return 'done';
		return 'idle';
	}

	function previewPath(scene: Scene): string | null {
		const job = jobForScene(scene.id);
		// While a new job is queued/running, don't keep showing the previous clip
		if (job && (job.status === 'pending' || job.status === 'running')) return null;
		if (job?.status === 'done') {
			const fromJob = (job.output_paths ?? []).find((p) => /\.(mp4|webm)$/i.test(p));
			if (fromJob) return fromJob;
		}
		if (scene.video_path && /\.(mp4|webm)$/i.test(scene.video_path)) return scene.video_path;
		return null;
	}

	type Thumb = { kind: 'image' | 'video'; src: string };

	function thumbFor(scene: Scene): Thumb | null {
		if (scene.env_image_path) {
			const src = assetUrl(scene.env_image_path);
			if (src) return { kind: 'image', src };
		}
		const preview = previewPath(scene);
		if (preview) {
			const src = assetUrl(preview);
			if (src) return { kind: 'video', src };
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

	const doneCount = $derived(scenes.filter((s) => statusOf(s) === 'done').length);

	async function togglePause() {
		if ($queueStatusQuery.data?.paused) await jobsApi.resume();
		else await jobsApi.pause();
		client.invalidateQueries({ queryKey: ['queue-status'] });
	}

	async function resumeQueue() {
		await jobsApi.resume();
		client.invalidateQueries({ queryKey: ['queue-status'] });
	}

	function selectScene(id: number) {
		selectedId = id;
	}

	// Filmstrip click → jump back to the editor with that scene selected.
	function editScene(id: number) {
		selectedId = id;
		view = 'edit';
	}

	function neighborId(dir: -1 | 1): number | null {
		const idx = scenes.findIndex((s) => s.id === selectedId);
		const next = scenes[(idx < 0 ? 0 : idx) + dir];
		return next?.id ?? null;
	}

	function step(dir: -1 | 1) {
		const id = neighborId(dir);
		if (id != null) selectedId = id;
	}

	const selJob = $derived(selected ? jobForScene(selected.id) : undefined);
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
	const clipsReady = $derived(scenes.filter((s) => Boolean(s.video_path)).length);
	const clipsMissing = $derived(scenes.length - clipsReady);

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
		if (exportState === 'active') return { status: 'running', label: 'Exporting' };
		if (exportState === 'ready') {
			return exportStale
				? { status: 'paused', label: 'Clips changed' }
				: { status: 'ready', label: 'Ready' };
		}
		if (exportState === 'failed') return { status: 'failed', label: 'Export failed' };
		return { status: 'idle', label: 'Not exported' };
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
			toast.success('Export queued');
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
		<h2>4. Video</h2>
		<p class="muted">
			{#if scenes.length === 0}
				Build a script first, then cut clips on the timeline.
			{:else}
				{doneCount}/{scenes.length} clips done · {formatClock(totalSec)} total
			{/if}
		</p>
	</div>
	<div class="actions">
		{#if scenes.length > 0}
			<Button
				variant="primary"
				disabled={batching || batchTargets.length === 0}
				loading={batching}
				title="Queue every scene's clip in timeline order; the worker renders them one at a time"
				onclick={generateAll}
			>
				<Icon name="film" size={14} /> {batchLabel}
			</Button>
		{/if}
		<Button variant="secondary" onclick={togglePause}>
			{$queueStatusQuery.data?.paused ? 'Resume queue' : 'Pause queue'}
		</Button>
		<div class="view-toggle" role="tablist" aria-label="Video stage view">
			<button
				type="button"
				role="tab"
				aria-selected={view === 'edit'}
				class="view-tab"
				class:active={view === 'edit'}
				onclick={() => (view = 'edit')}
			>
				<Icon name="edit" size={14} /> Edit
			</button>
			<button
				type="button"
				role="tab"
				aria-selected={view === 'film'}
				class="view-tab"
				class:active={view === 'film'}
				onclick={() => (view = 'film')}
			>
				<Icon name="film" size={14} /> Film
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
			<StatusChip status="paused" label="Queue paused" />
			<span class="paused-text">Renders are held — workers sit idle until you resume.</span>
			<span class="paused-action">
				<Button size="sm" variant="secondary" onclick={resumeQueue}>Resume now</Button>
			</span>
		</div>
	{/if}

	{#if scenes.length === 0}
		<div class="empty">
			<p class="empty-title">No timeline yet</p>
			<p class="muted">Generate or add scenes in Script, then come back to render clips.</p>
		</div>
	{:else if selected}
		{@const selWf = workflowFor(selected)}
		{@const selStatus = statusOf(selected)}
		{@const selPreview = previewPath(selected)}
		{@const selHasVideoInput = workflowHasVideoInput(selWf)}
		{@const selVideoNode = videoInputNodeFor(selWf)}
		{@const selClips = timelineClipOptions(selected)}
		{@const selChain = Boolean(selected.chain_from_prev)}
		{@const selSource = clipSource[selected.id] ?? 'auto'}
		{@const selSourceValid = selSource === 'auto' || selSource === 'upload' || selClips.some((s) => String(s.id) === selSource)}
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
			{selected}
			{selectedId}
			status={selStatus}
			previewPath={selPreview}
			progress={selProg}
			error={selError}
			errorLong={selErrorLong}
			job={selJob}
			sceneJobs={($jobsQuery.data ?? []).filter(
				(j) => j.scene_id === selected.id && j.kind === 'video',
			)}
			workflow={selWf}
			workflows={enabledWorkflows}
			bind:formValues
			{assetOptions}
			allowUpload
			submitting={$generateOne.isPending}
			{statusOf}
			{thumbFor}
			{formatClock}
			chained={(scene) => Boolean(scene.chain_from_prev)}
			generateDisabled={selBlocked}
			generateDisabledReason={selBlocked
				? 'This scene continues from the previous video — pick a workflow with a video input'
				: ''}
			clipSource={{
				enabled: selChain && selHasVideoInput && Boolean(selVideoNode),
				value: selSourceValid ? selSource : 'auto',
				options: selClips.map((s) => ({
					id: String(s.id),
					label: `#${s.order_index} ${s.heading || 'Scene'}`,
					path: s.video_path ?? undefined,
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
					const clip = selClips.find((s) => String(s.id) === val);
					if (clip?.video_path && selVideoNode) {
						formValues = { ...formValues, [selVideoNode.nodeId]: clip.video_path };
					}
				}
			}}
			onClipSourceUpload={() => videoFileInput?.click()}
			onSelect={selectScene}
			onStep={step}
			onWorkflowChange={(id) => {
				selectedWorkflow = { ...selectedWorkflow, [selected.id]: Number(id) };
			}}
		onGenerate={() => $generateOne.mutate({ sceneId: selected.id })}
		onPreviewPrompt={beginGenerate}
	/>

	<PromptPreviewModal
		bind:open={previewOpen}
		{projectId}
		scene={selected}
		workflow={selWf}
		inputValues={formValues}
		onConfirm={onGenerateConfirmed}
	/>
	{/if}
{:else}
	<section class="film-view" aria-label="Film screening room">
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
					label="Export unavailable"
				/>
				{#if exportStale}
					<div class="stale-banner" role="status">
						<Icon name="alert" size={14} />
						<span>Clips changed since this export — re-export to update.</span>
					</div>
				{/if}
			{:else if exportState === 'active'}
				<div class="program-slate">
					<Spinner size="lg" />
					<h3 class="slate-title">Exporting your film…</h3>
					<div class="slate-progress">
						<ProgressBar
							value={exportProg?.progress ?? 0}
							indeterminate={exportProg == null}
							label={exportProg?.message}
						/>
					</div>
					<p class="slate-sub">You can keep editing — export runs in the background.</p>
					<Button variant="ghost" size="sm" onclick={cancelExport}>Cancel</Button>
				</div>
			{:else if exportState === 'failed'}
				<div class="program-slate">
					<span class="slate-icon slate-icon-err" aria-hidden="true">
						<Icon name="alert" size={32} />
					</span>
					<h3 class="slate-title">Export failed</h3>
					<p class="slate-err" title={exportJob?.error ?? 'Export failed'}>
						{exportJob?.error || 'Export failed'}
					</p>
					<Button
						variant="secondary"
						loading={$exportFilm.isPending}
						onclick={() => $exportFilm.mutate()}
					>
						Retry export
					</Button>
				</div>
			{:else}
				<div class="program-slate">
					<span class="slate-icon" aria-hidden="true">
						<Icon name="film" size={32} />
					</span>
					<h3 class="slate-title">Your film isn't exported yet</h3>
					<p class="slate-sub">
						{clipsReady} clips · {formatClock(totalSec)} · 0.5s crossfades · loudness matched
					</p>
					{#if clipsMissing > 0}
						<p class="slate-warn" role="status">
							<Icon name="alert" size={14} />
							<span>
								{clipsMissing} scene{clipsMissing === 1 ? '' : 's'} without a clip will be skipped
							</span>
						</p>
					{/if}
					<Button
						variant="primary"
						disabled={clipsReady === 0}
						loading={$exportFilm.isPending}
						title={clipsReady === 0 ? 'Finish at least one clip to export a film' : 'Export film'}
						onclick={() => $exportFilm.mutate()}
					>
						<Icon name="film" size={14} /> Export film
					</Button>
				</div>
			{/if}
		</div>

		{#if exportState === 'ready'}
			{@const exportUrl = assetUrl(exportPath)}
			<div class="film-meta">
				<span class="film-meta-text">
					{exportClipCount} clips · {formatClock(totalSec)}{#if exportedAgo} · Exported {exportedAgo}{/if}
				</span>
				<div class="film-actions">
					{#if exportUrl}
						<a class="btn-dl" href={exportUrl} download={`${projectTitle}.mp4`}>
							<Icon name="download" size={14} /> Download film
						</a>
					{/if}
					<Button
						variant={exportStale ? 'primary' : 'ghost'}
						size="sm"
						loading={$exportFilm.isPending}
						onclick={() => $exportFilm.mutate()}
					>
						Re-export
					</Button>
				</div>
			</div>
		{/if}

		{#if scenes.length > 0}
			<div class="filmstrip-block">
				<p class="filmstrip-label">In this film</p>
				<div class="filmstrip">
					{#each scenes as scene (scene.id)}
						{@const thumb = thumbFor(scene)}
						<button
							type="button"
							class="filmstrip-item"
							title={`${scene.heading || 'Scene'} — edit in timeline`}
							onclick={() => editScene(scene.id)}
						>
							{#if thumb?.kind === 'image'}
								<img class="filmstrip-media" src={thumb.src} alt="" loading="lazy" />
							{:else if thumb?.kind === 'video'}
								<!-- svelte-ignore a11y_media_has_caption -->
								<video class="filmstrip-media" src={thumb.src} muted playsinline preload="metadata"></video>
							{:else}
								<span class="filmstrip-slate">#{scene.order_index}</span>
							{/if}
							<span class="filmstrip-num">#{scene.order_index}</span>
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
