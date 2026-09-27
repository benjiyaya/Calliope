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
	import ClipSourceModal from './ClipSourceModal.svelte';
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

	interface ClipSourceOption {
		/** Scene id as string, or the 'auto' / 'upload' sentinels. */
		id: string;
		label: string;
		/** Clip path — the source modal renders video thumbnails when present. */
		path?: string;
	}

	interface ClipSourceConfig {
		/** Only offered when the scene continues from the previous video and the workflow can accept it. */
		enabled: boolean;
		value: string;
		options: ClipSourceOption[];
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
		/** Disable Generate: clip continues from the previous video but the workflow cannot accept it. */
		generateDisabled?: boolean;
		generateDisabledReason?: string;
	/** Where this continue clip's video input comes from (auto / upload / a timeline clip). */
	clipSource?: ClipSourceConfig;
	onClipSourceChange?: (value: string) => void;
	/** Upload file picked in the source modal — caller opens the file dialog. */
	onClipSourceUpload?: () => void;
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
		generateDisabled = false,
		generateDisabledReason = '',
		clipSource,
		onClipSourceChange,
		onClipSourceUpload,
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

	let clipSourceOpen = $state(false);
	let inputsOpen = $state(false);

	const hasJobPayload = $derived(
		Boolean(
			job &&
				((typeof job.payload?.prompt === 'string' && job.payload.prompt) ||
					job.payload?.input_values),
		),
	);

	/** The label shown on the Video source trigger. */
	const clipSourceLabel = $derived.by(() => {
		if (!clipSource?.enabled) return '';
		const val = clipSource.value;
		if (val === 'auto') return t('clipSource.autoName');
		if (val === 'upload') return t('clipSource.uploadName');
		return clipSource.options.find((o) => o.id === val)?.label ?? t('clipSource.autoName');
	});
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
			{#if generateDisabled}
				<div class="continue-warning" role="alert">
					<Icon name="alert" size={16} />
					<div class="continue-warning-text">
						<span class="continue-warning-title">{t('videoEdit.noVideoInputWf')}</span>
						<span>{t('videoEdit.continueHint')}</span>
					</div>
				</div>
			{:else if clipSource?.enabled}
<div class="clip-source-row">
				<span class="clip-source-label" id="clip-source-label">{t('videoEdit.videoSource')}</span>
				<button
					type="button"
					class="clip-source-trigger"
					aria-haspopup="dialog"
					aria-expanded={clipSourceOpen}
					aria-labelledby="clip-source-label clip-source-value"
					onclick={() => (clipSourceOpen = true)}
				>
					<Icon name="film" size={14} />
					<span id="clip-source-value" class="clip-source-value">{clipSourceLabel}</span>
					<Icon name="chevron-down" size={12} />
				</button>
			</div>
			<ClipSourceModal
				bind:open={clipSourceOpen}
				value={clipSource.value}
				options={clipSource.options}
				onselect={(source) => onClipSourceChange?.(source)}
				onupload={() => onClipSourceUpload?.()}
			/>
		{/if}
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
				disabled={generateDisabled}
				generateDisabledHint={generateDisabledReason}
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

	.continue-warning {
		display: flex;
		align-items: flex-start;
		gap: 10px;
		padding: 10px 12px;
		margin: 0;
		border-radius: var(--radius-md);
		border: 1px solid color-mix(in srgb, var(--warning) 40%, var(--border));
		background: color-mix(in srgb, var(--warning) 10%, var(--bg-surface));
		color: var(--text-secondary);
		font-size: 13px;
	}

	.continue-warning :global(svg) {
		flex-shrink: 0;
		margin-top: 2px;
		color: var(--warning);
	}

	.continue-warning-text {
		display: flex;
		flex-direction: column;
		gap: 2px;
	}

	.continue-warning-title {
		font-weight: 650;
		color: var(--text-primary);
	}

	.clip-source-row {
		display: flex;
		align-items: center;
		gap: 8px;
		margin: 0;
	}

	.clip-source-label {
		font-size: 12px;
		color: var(--text-secondary);
		white-space: nowrap;
	}

	.clip-source-trigger {
		display: inline-flex;
		align-items: center;
		gap: 8px;
		max-width: 100%;
		min-width: 0;
		padding: 6px 12px;
		font: inherit;
		font-size: 13px;
		color: var(--text-primary);
		background: var(--bg-elevated);
		border: 1px solid var(--border);
		border-radius: var(--radius-sm);
		cursor: pointer;
	}

	.clip-source-trigger:hover {
		border-color: var(--text-muted);
	}

	.clip-source-trigger:focus-visible {
		outline: 2px solid var(--accent);
		outline-offset: 2px;
	}

	.clip-source-trigger :global(svg:last-child) {
		color: var(--text-muted);
	}

	.clip-source-value {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
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
