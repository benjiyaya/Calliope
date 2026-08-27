<script lang="ts">
	/**
	 * ClipMonitor — 16:9 hero preview for a scene clip.
	 * Letterboxes video; shows slate / progress / error when no preview.
	 */
	import { assetUrl } from '$lib/api';
	import SafeMedia from '$lib/components/SafeMedia.svelte';
	import Button from '$lib/components/ui/Button.svelte';
	import Icon from '$lib/components/ui/Icon.svelte';
	import ProgressBar from '$lib/components/ui/ProgressBar.svelte';
	import Spinner from '$lib/components/ui/Spinner.svelte';

	interface Progress {
		progress?: number;
		message?: string;
	}

	interface Props {
		previewPath: string | null;
		status: string;
		heading: string;
		orderIndex: number;
		sceneId?: number;
		progress?: Progress | null;
		error?: string;
		errorLong?: boolean;
	}

	let {
		previewPath,
		status,
		heading,
		orderIndex,
		sceneId,
		progress = null,
		error = '',
		errorLong = false,
	}: Props = $props();

	let errorOpen = $state(false);
	// '#t=0.1' media fragment: with preload="metadata" browsers paint NOTHING
	// until playback, so every clip preview sat black ("no picture, but sound").
	// The fragment makes the browser seek+paint a first frame without playing.
	const previewUrl = $derived(previewPath ? assetUrl(previewPath) + '#t=0.1' : null);

	$effect(() => {
		void previewPath;
		void error;
		errorOpen = false;
	});
</script>

<div class="monitor">
	<div class="frame">
		{#if previewUrl}
			<SafeMedia class="media" src={previewUrl} kind="video" label="Video unavailable" />
		{:else}
			<div class="empty">
				<span class="slate">#{orderIndex}</span>
				{#if sceneId != null}
					<p class="sid">scene_id {sceneId}</p>
				{/if}
				<p class="title">{heading || 'Untitled'}</p>
				{#if status === 'pending' || status === 'running'}
					<div class="busy" aria-busy="true">
						<div class="busy-head">
							<Spinner size="sm" />
							<span>{status === 'running' ? 'Generating…' : 'Queued — waiting for a worker'}</span>
						</div>
						{#if status === 'running'}
							<ProgressBar
								size="sm"
								value={progress?.progress ?? 0}
								indeterminate={progress == null}
								label={progress?.message}
							/>
						{/if}
					</div>
				{:else if status === 'failed'}
					<div class="fail">
						<p class="err" class:open={errorOpen}>{error || 'Generation failed'}</p>
						{#if errorLong}
							<Button variant="ghost" size="sm" onclick={() => (errorOpen = !errorOpen)}>
								{errorOpen ? 'Hide details' : 'Show details'}
							</Button>
						{/if}
					</div>
				{:else}
					<p class="hint">No render yet — describe the shot below and generate.</p>
				{/if}
			</div>
		{/if}
	</div>

	<div class="ids">
		<span>#{orderIndex}</span>
		{#if sceneId != null}
			<span class="ids-db">id {sceneId}</span>
		{/if}
	</div>
	{#if previewUrl}
		<div class="foot">
			<a class="dl" href={previewUrl} download>
				<Icon name="download" size={14} /> Download clip
			</a>
		</div>
	{/if}
</div>

<style>
	.monitor {
		display: flex;
		flex-direction: column;
		width: 100%;
		height: 100%;
		min-height: 0;
	}

	.frame {
		flex: 1;
		min-height: 0;
		width: 100%;
		height: 100%;
		background:
			radial-gradient(ellipse at center, rgba(139, 92, 246, 0.08), transparent 55%),
			#050508;
		border: 1px solid var(--border);
		border-radius: var(--radius-md);
		overflow: hidden;
		display: flex;
		align-items: center;
		justify-content: center;
	}

	.frame :global(.media) {
		width: 100%;
		height: 100%;
		object-fit: contain;
		background: #000;
		border: none;
		border-radius: 0;
		min-height: 0;
	}

	.empty {
		padding: 24px;
		text-align: center;
		max-width: 42ch;
	}

	.slate {
		display: inline-block;
		font-family: var(--font-mono);
		font-size: 22px;
		font-weight: 700;
		color: var(--accent);
		margin-bottom: 6px;
	}

	.sid {
		margin: 0 0 8px;
		font-family: var(--font-mono);
		font-size: 11px;
		font-weight: 600;
		letter-spacing: 0.04em;
		color: var(--text-muted);
	}

	.title {
		margin: 0 0 8px;
		font-size: 14px;
		font-weight: 600;
		color: var(--text-primary);
	}

	.hint {
		margin: 0;
		font-size: 13px;
		color: var(--text-muted);
		line-height: 1.45;
	}

	.busy {
		margin-top: 12px;
		display: flex;
		flex-direction: column;
		gap: 8px;
		text-align: left;
	}

	.busy-head {
		display: flex;
		align-items: center;
		justify-content: center;
		gap: 8px;
		font-size: 13px;
		font-weight: 600;
		color: var(--warning);
	}

	.fail {
		margin-top: 8px;
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: 4px;
	}

	.err {
		margin: 0;
		font-size: 13px;
		font-weight: 600;
		color: var(--error);
		display: -webkit-box;
		-webkit-line-clamp: 3;
		line-clamp: 3;
		-webkit-box-orient: vertical;
		overflow: hidden;
		word-break: break-word;
		white-space: pre-wrap;
	}

	.err.open {
		display: block;
		overflow: visible;
		max-height: 160px;
		overflow-y: auto;
	}

	.ids {
		display: flex;
		align-items: center;
		gap: 10px;
		padding: 6px 2px 0;
		font-family: var(--font-mono);
		font-size: 12px;
		font-weight: 700;
		color: var(--accent);
		flex-shrink: 0;
	}

	.ids-db {
		font-weight: 600;
		color: var(--text-muted);
	}

	.foot {
		display: flex;
		justify-content: flex-end;
		padding-top: 8px;
		flex-shrink: 0;
	}

	.dl {
		display: inline-flex;
		align-items: center;
		gap: 6px;
		font-size: 12px;
		font-weight: 600;
		color: var(--text-secondary);
		text-decoration: none;
	}

	.dl:hover {
		color: var(--accent);
	}
</style>
