<script lang="ts">
	/**
	 * VideoEditWorkspace — modern Edit layout for Project → Video.
	 * Hero monitor + filmstrip + meta strip + docked Omni composer.
	 * Does not reuse the old top two-card clip-stage layout.
	 */
	import type { Job, Scene, Workflow } from '$lib/api';
	import OmniComposer from '$lib/components/OmniComposer.svelte';
	import type { AssetOption } from '$lib/assetPicker';
	import Icon from '$lib/components/ui/Icon.svelte';
	import ClipMonitor from './ClipMonitor.svelte';
	import ClipSourceModal from './ClipSourceModal.svelte';
	import JobInputsDrawer from './JobInputsDrawer.svelte';
	import PromptPreviewModal from './PromptPreviewModal.svelte';
	import SceneFilmstrip from './SceneFilmstrip.svelte';
	import SceneScriptDrawer from './SceneScriptDrawer.svelte';

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
		selected: Scene;
		selectedId: number | null;
		status: string;
		previewPath: string | null;
		progress?: Progress | null;
		error?: string;
		errorLong?: boolean;
	/** Latest video job for the scene — drives the "what was sent" drawer. */
	job?: Job | null;
	/** All video jobs for the scene (history strip in the drawer). */
	sceneJobs?: Job[];
		workflow: Workflow | undefined;
		workflows: Workflow[];
		formValues: Record<string, string | number>;
		assetOptions: AssetOption[];
		allowUpload?: boolean;
		/** Disable Generate: scene continues from the previous video but the workflow cannot accept it. */
		generateDisabled?: boolean;
		generateDisabledReason?: string;
	/** Where this continue scene's video input comes from (auto / upload / a timeline clip). */
	clipSource?: ClipSourceConfig;
	onClipSourceChange?: (value: string) => void;
	/** Upload file picked in the source modal — caller opens the file dialog. */
	onClipSourceUpload?: () => void;
	/** HITL prompt review before Generate: caller resolves + shows the modal. */
	onPreviewPrompt?: () => void;
	generateLabel?: string;
		chained?: (scene: Scene) => boolean;
		submitting?: boolean;
		statusOf: (scene: Scene) => string;
		thumbFor: (scene: Scene) => Thumb;
		formatClock: (sec: number) => string;
		onSelect: (id: number) => void;
		onStep: (dir: -1 | 1) => void;
		onWorkflowChange: (id: number) => void;
		onFormChange?: (values: Record<string, string | number>) => void;
		onGenerate: () => void;
	}

	let {
		scenes,
		selected,
		selectedId,
		status,
		previewPath,
		progress = null,
		error = '',
		errorLong = false,
		job = null,
		sceneJobs = [],
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
		generateLabel = 'Generate clip',
		chained = () => false,
		submitting = false,
		statusOf,
		thumbFor,
		formatClock,
		onSelect,
		onStep,
		onWorkflowChange,
		onFormChange,
		onGenerate,
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
		if (val === 'auto') return 'Auto (previous clip)';
		if (val === 'upload') return 'Upload file';
		return clipSource.options.find((o) => o.id === val)?.label ?? 'Auto (previous clip)';
	});
</script>

<div class="workspace">
	<div class="hero">
		<ClipMonitor
			{previewPath}
			{status}
			heading={selected.heading || 'Untitled'}
			orderIndex={selected.order_index}
			sceneId={selected.id}
			{progress}
			{error}
			{errorLong}
		/>
	</div>

	<SceneFilmstrip
		{scenes}
		{selectedId}
		{statusOf}
		{thumbFor}
		{formatClock}
		{chained}
		{onSelect}
		{onStep}
	/>

	<SceneScriptDrawer scene={selected} {status} {formatClock} />

	<div class="composer-dock">
		{#if workflow}
			{#if generateDisabled}
				<div class="continue-warning" role="alert">
					<Icon name="alert" size={16} />
					<div class="continue-warning-text">
						<span class="continue-warning-title">Workflow has no video input</span>
						<span>
							This scene continues from the previous video. Switch to a workflow that has a video
							input (LoadVideo node tagged (Input:video)).
						</span>
					</div>
				</div>
			{:else if clipSource?.enabled}
			<div class="clip-source-row">
				<span class="clip-source-label" id="clip-source-label">Video source</span>
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
			<p class="asset-hint">
				No refs yet. Generate character sheets or environments in Assets, or upload a video/audio
				file here.
			</p>
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
					<span>View prompt &amp; inputs</span>
				</button>
			</div>
			<JobInputsDrawer
				bind:open={inputsOpen}
				{job}
				jobs={sceneJobs}
				{workflow}
				onCopySettings={(values) => {
					formValues = { ...formValues, ...values };
					onFormChange?.({ ...formValues });
				}}
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
			{generateLabel}
			{submitting}
			disabled={generateDisabled}
			generateDisabledHint={generateDisabledReason}
			onChange={onFormChange}
			onSubmit={onPreviewPrompt ?? onGenerate}
		/>
		{:else}
			<div class="no-wf">
				<p class="empty-title">No video workflow enabled</p>
				<p class="muted">
					Enable a video workflow in <a href="/settings?tab=workflows">Settings → Workflows</a>.
				</p>
			</div>
		{/if}
	</div>
</div>

<style>
	.workspace {
		flex: 1;
		min-height: 0;
		display: flex;
		flex-direction: column;
		gap: 10px;
		overflow: hidden;
	}

	.hero {
		flex: 1;
		min-height: 140px;
		display: flex;
		align-items: stretch;
		justify-content: stretch;
		overflow: hidden;
		width: 100%;
	}

	.composer-dock {
		flex-shrink: 0;
		min-height: 0;
	}

	.composer-dock :global(.omni-shell) {
		flex-shrink: 0;
	}

	.continue-warning {
		display: flex;
		align-items: flex-start;
		gap: 10px;
		padding: 10px 12px;
		margin: 0 0 8px;
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
		margin: 0 0 8px;
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
		max-width: 360px;
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
		margin: 0 0 8px;
		font-size: 12px;
		color: var(--text-muted);
		line-height: 1.4;
	}

	.job-inputs-row {
		display: flex;
		justify-content: flex-end;
		margin: 0 0 6px;
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
