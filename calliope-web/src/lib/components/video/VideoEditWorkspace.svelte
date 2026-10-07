<script lang="ts">
	/**
	 * VideoEditWorkspace — modern Edit layout for Project → Video.
	 * Hero monitor + filmstrip + meta strip + docked Omni composer.
	 * Does not reuse the old top two-card clip-stage layout.
	 */
	import type { Clip, Job, Scene, Workflow } from '$lib/api';
	import OmniComposer from '$lib/components/OmniComposer.svelte';
	import type { AssetOption } from '$lib/assetPicker';
	import { carryValuesAcrossWorkflows, sanitizeWorkflowValues } from '$lib/comfy/carryValues';
	import Icon from '$lib/components/ui/Icon.svelte';
	import ClipMonitor from './ClipMonitor.svelte';
	import JobInputsDrawer from './JobInputsDrawer.svelte';
	import PromptPreviewModal from './PromptPreviewModal.svelte';
	import SceneFilmstrip, { type FilmstripClip } from './SceneFilmstrip.svelte';
	import SceneScriptDrawer from './SceneScriptDrawer.svelte';
import { t } from '$lib/i18n.svelte';
	import ShotBrief from './ShotBrief.svelte';

	type Thumb = { kind: 'image' | 'video'; src: string } | null;

	interface Progress {
		progress?: number;
		message?: string;
	}

	interface Props {
		scenes: Scene[];
		/** Flattened clips in playback order — the filmstrip + selection units. */
		filmClips: FilmstripClip[];
		/** Selected clip (defaults to the scene's first when the scene has one clip). */
		selectedClip: { clip: Clip; scene: Scene; index: number; label: string } | null;
		selectedClipId: number | null;
		selected: Scene;
		status: string;
		previewPath: string | null;
		progress?: Progress | null;
		error?: string;
		errorLong?: boolean;
	/** Latest video job for the selected clip — drives the "what was sent" drawer. */
	job?: Job | null;
	/** All video jobs for the selected clip (history strip in the drawer). */
	clipJobs?: Job[];
		workflow: Workflow | undefined;
		workflows: Workflow[];
		formValues: Record<string, string | number>;
		assetOptions: AssetOption[];
		allowUpload?: boolean;
		/** HITL prompt review before Generate: caller resolves + shows the modal. */
		onPreviewPrompt?: () => void;
		generateLabel?: string;
		submitting?: boolean;
		statusOfClip: (clipId: number) => string;
		thumbForClip: (clipId: number) => Thumb;
		formatClock: (sec: number) => string;
		onSelectClip: (clipId: number) => void;
		onStep: (dir: -1 | 1) => void;
	onWorkflowChange: (id: number) => void;
	onFormChange?: (values: Record<string, string | number>) => void;
	onGenerate: () => void;
	/** Render-history versioning: apply an older job's output to the clip. */
	onApplyToClip?: (job: Job, path: string) => void;
	applying?: boolean;
	}

	let {
		scenes,
		filmClips,
		selectedClip,
		selectedClipId,
		selected,
		status,
		previewPath,
		progress = null,
		error = '',
		errorLong = false,
		job = null,
		clipJobs = [],
		workflow,
		workflows,
		formValues = $bindable(),
		assetOptions,
		allowUpload = true,
		onPreviewPrompt,
		generateLabel = '',
		submitting = false,
		statusOfClip,
		thumbForClip,
		formatClock,
		onSelectClip,
		onStep,
		onWorkflowChange,
		onFormChange,
		onGenerate,
		onApplyToClip,
		applying = false,
	}: Props = $props();

	let inputsOpen = $state(false);

	const hasJobPayload = $derived(
		Boolean(
			job &&
				((typeof job.payload?.prompt === 'string' && job.payload.prompt) ||
					job.payload?.input_values),
		),
	);
</script>

<div class="workspace">
<div class="preview-col">
		<div class="hero">
			<ClipMonitor
				{previewPath}
				{status}
				heading={(selectedClip?.clip.description || selected.heading || t('clipMonitor.untitled')).slice(0, 80)}
				orderIndex={selected.order_index}
				label={selectedClip?.label}
				idLabel={selectedClip ? t('videoEdit.clipId', { id: selectedClip.clip.id }) : selected ? t('videoEdit.sceneId', { id: selected.id }) : undefined}
				sceneId={selectedClip?.scene.id ?? selected?.id}
				{progress}
				{error}
				{errorLong}
			/>
		</div>

		<SceneFilmstrip
			clips={filmClips}
			{selectedClipId}
			{statusOfClip}
			{thumbForClip}
			{formatClock}
			{onSelectClip}
			{onStep}
		/>

		<SceneScriptDrawer scene={selected} {status} {formatClock} />
	</div>

	<aside class="dock-col" aria-label={t('videoEdit.dockAria')}>
		{#if workflow}
			{#if assetOptions.length === 0}
				<p class="asset-hint">{t('videoEdit.assetHint')}</p>
			{/if}
		{#if hasJobPayload}
			<div class="job-inputs-row">
				<button
					type="button"
					class="job-inputs-trigger"
					aria-haspopup="dialog"
					aria-expanded={inputsOpen}
					onclick={() => (inputsOpen = true)}
				>
					<Icon name="info" size={14} />
					<span>{t('videoEdit.viewPrompt')}</span>
				</button>
			</div>
			<JobInputsDrawer
				bind:open={inputsOpen}
				{job}
				jobs={clipJobs}
				{workflow}
				sceneVideoPath={selectedClip?.clip.clip_path ?? selected.video_path}
				onCopySettings={(values, jobWorkflowId) => {
					// A job from another workflow keys its values by THAT workflow's nodeIds.
					const source =
						jobWorkflowId != null && jobWorkflowId !== workflow?.id
							? workflows.find((w) => w.id === jobWorkflowId)?.input_schema
							: workflow?.input_schema;
					const copied = carryValuesAcrossWorkflows(values, source, workflow?.input_schema);
					formValues = sanitizeWorkflowValues({ ...formValues, ...copied }, workflow?.input_schema);
					onFormChange?.({ ...formValues });
				}}
				onApplyToScene={(j, path) => onApplyToClip?.(j, path)}
				applying={applying}
			/>
		{/if}
{#if selectedClip}
				<ShotBrief
					clip={selectedClip.clip}
					scene={selectedClip.scene}
					label={selectedClip.label}
					{formatClock}
				/>
			{/if}
			<OmniComposer
				inputs={workflow.input_schema}
				bind:values={formValues}
				{workflow}
				{workflows}
				onWorkflowChange={onWorkflowChange}
{assetOptions}
				{allowUpload}
				generateLabel={generateLabel || t('videoEdit.generateLabel')}
				{submitting}
				onChange={onFormChange}
				onSubmit={onPreviewPrompt ?? onGenerate}
			/>
		{:else}
			<div class="no-wf">
				<p class="empty-title">{t('videoEdit.noWf')}</p>
				<p class="muted">
					{t('videoEdit.enableWfPre')} <a href="/settings?tab=workflows">{t('videoEdit.wfLink')}</a>{t('videoEdit.enableWfPost')}
				</p>
			</div>
		{/if}
	</aside>
</div>

<style>
	/* Two columns: the player + strip own the left, every generation input
	   lives in the right inspector so it can't push the player out of view. */
	.workspace {
		flex: 1;
		min-height: 0;
		display: flex;
		flex-direction: row;
		gap: 12px;
		overflow: hidden;
	}

	.preview-col {
		flex: 1 1 auto;
		min-width: 0;
		display: flex;
		flex-direction: column;
		gap: 10px;
		overflow: hidden;
	}

	.hero {
		flex: 1 1 auto;
		min-height: 200px;
		display: flex;
		align-items: stretch;
		justify-content: stretch;
		overflow: hidden;
		width: 100%;
	}

	/* Inspector — scrolls on its own so the player keeps the height. */
	.dock-col {
		flex: 0 0 clamp(300px, 34%, 400px);
		min-width: 280px;
		display: flex;
		flex-direction: column;
		gap: 8px;
		overflow-y: auto;
		overscroll-behavior: contain;
		padding-right: 2px;
	}

	.dock-col :global(.omni-shell) {
		flex-shrink: 0;
	}

	/* Very narrow: stack, player first. */
	@media (max-width: 900px) {
		.workspace {
			flex-direction: column;
			overflow-y: auto;
		}

		.preview-col {
			overflow: visible;
		}

		.dock-col {
			flex: 0 0 auto;
			overflow: visible;
		}
	}

	.asset-hint {
		margin: 0;
		font-size: 12px;
		color: var(--text-muted);
		line-height: 1.4;
	}

	.job-inputs-row {
		display: flex;
		justify-content: flex-end;
		margin: 0;
	}

	.job-inputs-trigger {
		display: inline-flex;
		align-items: center;
		gap: 6px;
		padding: 4px 10px;
		font: inherit;
		font-size: 12px;
		font-weight: 500;
		color: var(--text-secondary);
		background: transparent;
		border: 1px solid var(--border);
		border-radius: var(--radius-sm);
		cursor: pointer;
		transition:
			color 150ms ease,
			border-color 150ms ease;
	}

	.job-inputs-trigger:hover {
		color: var(--text-primary);
		border-color: var(--text-muted);
	}

	.job-inputs-trigger:focus-visible {
		outline: 2px solid var(--accent);
		outline-offset: 2px;
	}

	.no-wf {
		padding: 20px;
		text-align: center;
		border: 1px dashed var(--border);
		border-radius: var(--radius-lg);
		background: var(--bg-surface);
	}

	.empty-title {
		margin: 0 0 4px;
		font-size: 14px;
		font-weight: 600;
		color: var(--text-primary);
	}

	.muted {
		margin: 0;
		font-size: 13px;
		color: var(--text-secondary);
	}

	.muted a {
		color: var(--accent);
	}
</style>
