<script lang="ts">
	import { assetUrl } from '$lib/api';
	import SafeMedia from '$lib/components/SafeMedia.svelte';
	import ImageLightbox from '$lib/components/ImageLightbox.svelte';
	import Icon from '$lib/components/ui/Icon.svelte';

	export interface ArtifactJob {
		id: number;
		kind: string;
		status: string;
		output_paths?: string[];
		error?: string | null;
	}

	interface Props {
		job: ArtifactJob;
	}

	let { job }: Props = $props();

	let preview = $state<{ src: string; kind: 'image' | 'video' } | null>(null);

	const primaryPath = $derived(job.output_paths?.[0] ?? null);
	const primaryMedia = $derived(primaryPath ? assetUrl(primaryPath) : null);

	function isVideoPath(path: string, kind: string): boolean {
		const lower = path.toLowerCase();
		return kind === 'video' || lower.endsWith('.mp4') || lower.endsWith('.webm');
	}

	const isVideo = $derived(primaryPath ? isVideoPath(primaryPath, job.kind) : job.kind === 'video');

	function statusWord(status: string): string {
		if (status === 'done') return 'Ready';
		if (status === 'running') return 'Generating';
		if (status === 'pending') return 'Queued';
		if (status === 'failed') return 'Failed';
		return status;
	}

	const busy = $derived(job.status === 'pending' || job.status === 'running');
	const failed = $derived(job.status === 'failed');
</script>

<div class="artifact" class:done={job.status === 'done'} class:failed>
	<div class="meta">
		<span class="id mono">#{job.id}</span>
		<span class="badge {job.status}">{statusWord(job.status)}</span>
		<span class="kind muted">
			<Icon name={job.kind === 'video' ? 'video' : 'image'} size={12} />
			{job.kind}
		</span>
	</div>

	{#if busy}
		<div class="waiting" aria-busy="true">
			<span class="pulse"></span>
			<span class="waiting-text">{job.status === 'running' ? 'Generating…' : 'Queued'}</span>
		</div>
	{/if}

	{#if failed && job.error}
		<p class="err">{job.error}</p>
	{/if}

	{#if primaryMedia && primaryPath}
		{#if isVideo}
			<button
				type="button"
				class="media-hit"
				aria-label="Play video, artifact {job.id}"
				title="Play in viewer"
				onclick={() => (preview = { src: primaryMedia, kind: 'video' })}
			>
				<div class="media-frame">
					<SafeMedia
						class="artifact-media"
						src={primaryMedia}
						kind="video"
						label="Video unavailable"
						controls={false}
					/>
					<span class="play-badge" aria-hidden="true"><Icon name="play" size={20} /></span>
				</div>
			</button>
		{:else}
			<button
				type="button"
				class="media-hit"
				aria-label="View image, artifact {job.id}"
				onclick={() => (preview = { src: primaryMedia, kind: 'image' })}
			>
				<div class="media-frame">
					<SafeMedia
						class="artifact-media"
						src={primaryMedia}
						alt="Artifact {job.id}"
						label="Image unavailable"
					/>
				</div>
			</button>
		{/if}
	{:else if job.status === 'done'}
		<div class="missing">
			<p>File missing on disk</p>
		</div>
	{/if}
</div>

<ImageLightbox
	src={preview?.src ?? null}
	alt={`Artifact #${job.id}`}
	kind={preview?.kind ?? 'image'}
	onClose={() => (preview = null)}
/>

<style>
	.artifact {
		border: 1px solid var(--border);
		border-radius: var(--radius-md);
		background: var(--bg-surface);
		padding: 10px;
		display: flex;
		flex-direction: column;
		gap: 8px;
		min-width: 0;
		max-width: 260px;
		overflow: hidden;
	}
	.artifact.done {
		border-color: color-mix(in srgb, var(--success) 30%, var(--border));
	}
	.artifact.failed {
		border-color: rgba(239, 68, 68, 0.4);
	}
	.meta {
		display: flex;
		align-items: center;
		gap: 8px;
	}
	.id {
		font-size: 12px;
		color: var(--text-muted);
	}
	.kind {
		font-size: 11px;
		margin-left: auto;
		display: inline-flex;
		align-items: center;
		gap: 4px;
	}
	.badge {
		display: inline-flex;
		align-items: center;
		padding: 2px 8px;
		border-radius: 9999px;
		font-size: 11px;
		font-weight: 600;
	}
	.badge.done {
		background: color-mix(in srgb, var(--success) 15%, transparent);
		color: var(--success);
	}
	.badge.failed {
		background: color-mix(in srgb, var(--error) 15%, transparent);
		color: var(--error);
	}
	.badge.pending,
	.badge.running {
		background: color-mix(in srgb, var(--warning) 15%, transparent);
		color: var(--warning);
	}
	.err {
		color: var(--error);
		font-size: 12px;
		margin: 0;
	}
	.waiting {
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 6px 2px;
	}
	.waiting-text {
		font-size: 12.5px;
		color: var(--text-secondary);
	}
	.pulse {
		width: 8px;
		height: 8px;
		flex-shrink: 0;
		border-radius: 9999px;
		background: var(--warning);
		animation: pulse 1.2s ease-in-out infinite;
	}
	@keyframes pulse {
		0%,
		100% {
			opacity: 0.4;
		}
		50% {
			opacity: 1;
		}
	}
	.media-hit {
		padding: 0;
		border: none;
		background: transparent;
		cursor: zoom-in;
		display: block;
		width: 100%;
	}
	.media-hit:has(:global(video)) {
		cursor: pointer;
	}
	.media-hit:focus-visible {
		outline: 2px solid var(--accent);
		outline-offset: 2px;
		border-radius: var(--radius-sm);
	}
	.media-frame {
		position: relative;
		width: 100%;
		aspect-ratio: 4 / 3;
		border-radius: var(--radius-sm);
		overflow: hidden;
		background: var(--bg-elevated);
	}
	.media-frame :global(.artifact-media),
	.media-frame :global(img),
	.media-frame :global(video) {
		width: 100%;
		height: 100%;
		object-fit: cover;
		display: block;
		border-radius: 0;
	}
	.play-badge {
		position: absolute;
		inset: 0;
		display: flex;
		align-items: center;
		justify-content: center;
		pointer-events: none;
	}
	.play-badge::before {
		content: '';
		width: 44px;
		height: 44px;
		border-radius: 9999px;
		background: rgba(0, 0, 0, 0.55);
		box-shadow: 0 0 0 1px rgba(255, 255, 255, 0.18);
	}
	.play-badge :global(svg) {
		position: absolute;
		color: #fff;
		fill: currentColor;
		filter: drop-shadow(0 1px 2px rgba(0, 0, 0, 0.6));
		margin-left: 2px;
	}
	.missing {
		padding: 10px;
		border-radius: var(--radius-sm);
		background: var(--bg-elevated);
		color: var(--text-muted);
		font-size: 12.5px;
	}
	.missing p {
		margin: 0;
	}
</style>
