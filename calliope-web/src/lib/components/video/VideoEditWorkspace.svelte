<script lang="ts">
	/**
	 * VideoEditWorkspace — modern Edit layout for Project → Video.
	 * Hero monitor + filmstrip + meta strip + docked Omni composer.
	 * Does not reuse the old top two-card clip-stage layout.
	 */
	import type { Scene, Workflow } from '$lib/api';
	import OmniComposer from '$lib/components/OmniComposer.svelte';
	import type { AssetOption } from '$lib/assetPicker';
	import Icon from '$lib/components/ui/Icon.svelte';
	import ClipMonitor from './ClipMonitor.svelte';
	import SceneFilmstrip from './SceneFilmstrip.svelte';
	import SceneScriptDrawer from './SceneScriptDrawer.svelte';

	type Thumb = { kind: 'image' | 'video'; src: string } | null;

	interface Progress {
		progress?: number;
		message?: string;
	}

	interface ClipSourceOption {
		/** Scene id as string, for a native select. */
		id: string;
		label: string;
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
		workflow,
		workflows,
		formValues = $bindable(),
		assetOptions,
		allowUpload = true,
		generateDisabled = false,
		generateDisabledReason = '',
		clipSource,
		onClipSourceChange,
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
					<label class="clip-source-label" for="clip-source-select">Video source</label>
					<select
						id="clip-source-select"
						class="clip-source-select"
						value={clipSource.value}
						onchange={(e) => onClipSourceChange?.(e.currentTarget.value)}
					>
						<option value="auto">Auto (previous clip)</option>
						<option value="upload">Upload file</option>
						{#if clipSource.options.length > 0}
							<optgroup label="From timeline">
								{#each clipSource.options as opt (opt.id)}
									<option value={opt.id}>{opt.label}</option>
								{/each}
							</optgroup>
						{/if}
					</select>
				</div>
			{/if}
			{#if assetOptions.length === 0}
				<p class="asset-hint">
					No refs yet. Generate character sheets or environments in Assets, or upload a video/audio
					file here.
				</p>
			{/if}
			<OmniComposer
				inputs={workflow.input_schema}
				bind:values={formValues}
				{workflow}
				{workflows}
				onWorkflowChange={onWorkflowChange}
			{assetOptions}
			{allowUpload}
			generateLabel="Generate clip"
			{submitting}
			disabled={generateDisabled}
			generateDisabledHint={generateDisabledReason}
			onChange={onFormChange}
			onSubmit={onGenerate}
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

	.clip-source-select {
		max-width: 320px;
		padding: 6px 10px;
		font-size: 13px;
		color: var(--text-primary);
		background: var(--bg-elevated);
		border: 1px solid var(--border);
		border-radius: var(--radius-sm);
		font-family: var(--font-body);
	}

	.asset-hint {
		margin: 0 0 8px;
		font-size: 12px;
		color: var(--text-muted);
		line-height: 1.4;
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
