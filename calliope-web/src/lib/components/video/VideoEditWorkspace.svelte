<script lang="ts">
	/**
	 * VideoEditWorkspace — modern Edit layout for Project → Video.
	 * Hero monitor + filmstrip + meta strip + docked Omni composer.
	 * Does not reuse the old top two-card clip-stage layout.
	 */
	import type { Scene, Workflow } from '$lib/api';
	import OmniComposer from '$lib/components/OmniComposer.svelte';
	import ClipMonitor from './ClipMonitor.svelte';
	import SceneFilmstrip from './SceneFilmstrip.svelte';
	import SceneScriptDrawer from './SceneScriptDrawer.svelte';

	type Thumb = { kind: 'image' | 'video'; src: string } | null;

	interface AssetOption {
		label: string;
		path: string;
		kind?: 'image' | 'video' | 'audio';
	}

	interface Progress {
		progress?: number;
		message?: string;
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
		showContinueMotion?: boolean;
		continueMotion?: boolean;
		onContinueChange?: (on: boolean) => void;
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
		showContinueMotion = false,
		continueMotion = false,
		onContinueChange,
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
			{#if assetOptions.length === 0}
				<p class="asset-hint">
					No refs yet. Generate character sheets or environments in Assets, or upload a video/audio
					file here.
				</p>
			{/if}
			{#if showContinueMotion}
				<label class="continue-row">
					<input
						type="checkbox"
						checked={continueMotion}
						onchange={(e) => onContinueChange?.(e.currentTarget.checked)}
					/>
					<span>Continue motion from previous clip</span>
				</label>
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

	.continue-row {
		display: flex;
		align-items: center;
		gap: 8px;
		margin: 0 0 8px;
		font-size: 12px;
		color: var(--text-secondary);
		cursor: pointer;
	}

	.continue-row input {
		accent-color: var(--accent);
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
