<script lang="ts">
	import { type Node, type NodeProps } from '@xyflow/svelte';
	import SafeMedia from '$lib/components/SafeMedia.svelte';
	import { assetUrl } from '$lib/api';

	interface ArtifactNodeData extends Record<string, unknown> {
		canvasNodeId: number;
		title: string;
		/** Generated media file (image or video) — absolute path served via /assets. */
		artifactPath: string | null;
		kind: 'image' | 'video';
		status: string;
		/** Called on media click: opens the large preview (no navigation). */
		onOpenMedia?: (kind: 'image' | 'video') => void;
	}

	type ArtifactNode = Node<ArtifactNodeData, 'image' | 'video'>;

	let { data }: NodeProps<ArtifactNode> = $props();

	const mediaSrc = $derived(data.artifactPath ? assetUrl(data.artifactPath) : null);
	// '#t=0.1' fragment paints the first frame (same trick as ClipMonitor).
	const videoSrc = $derived(
		data.kind === 'video' && mediaSrc ? mediaSrc + '#t=0.1' : null,
	);

	const running = $derived(data.status === 'running' || data.status === 'pending');

	function openPreview() {
		if (!mediaSrc) return;
		data.onOpenMedia?.(data.kind);
	}
</script>

<div class="artifact-node" class:running>
	<header>
		<span class="type-chip">{data.kind}</span>
		<span class="title" title={data.title}>{data.title}</span>
		{#if running}
			<span class="status running-badge">running</span>
		{:else if data.status === 'failed'}
			<span class="status failed-badge">failed</span>
		{:else if videoSrc}
			<span class="play-badge" aria-hidden="true">▶</span>
		{/if}
	</header>
	<div class="media">
		{#if videoSrc}
			<!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_static_element_interactions -->
			<button
				type="button"
				class="media-btn"
				onclick={openPreview}
				title="Open large preview"
			>
				<!-- svelte-ignore a11y_media_has_caption -->
				<video
					class="media-el"
					src={videoSrc}
					muted
					playsinline
					preload="metadata"
				></video>
			</button>
		{:else if mediaSrc}
			<!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_static_element_interactions -->
			<button
				type="button"
				class="media-btn"
				onclick={openPreview}
				title="Open large preview"
			>
				<SafeMedia
					class="media-el"
					src={mediaSrc}
					alt={data.title}
					kind="image"
					controls={false}
				/>
			</button>
		{:else if data.status === 'running'}
			<span class="placeholder">generating…</span>
		{:else}
			<span class="placeholder">no media</span>
		{/if}
	</div>
	<span class="out-kind">{data.kind}</span>
</div>

<style>
	.artifact-node {
		width: 220px;
		background: var(--bg-surface);
		border: 1px solid var(--border);
		border-radius: 12px;
		overflow: hidden;
		position: relative;
	}
	.artifact-node.running {
		border-color: rgba(125, 211, 252, 0.6);
		box-shadow: 0 0 18px rgba(125, 211, 252, 0.25);
	}
	:global(.svelte-flow__node.selected) .artifact-node {
		border-color: var(--accent);
		box-shadow:
			0 0 0 1px var(--accent),
			0 0 30px var(--accent-glow);
	}
	header {
		display: flex;
		align-items: center;
		gap: 7px;
		padding: 8px 10px;
	}
	.type-chip {
		font-size: 9px;
		letter-spacing: 0.06em;
		text-transform: uppercase;
		color: #bae6fd;
		background: rgba(12, 74, 110, 0.55);
		border: 1px solid rgba(125, 211, 252, 0.35);
		border-radius: 999px;
		padding: 2px 6px;
		flex-shrink: 0;
	}
	.title {
		flex: 1;
		min-width: 0;
		font-size: 12px;
		font-weight: 500;
		color: var(--text-primary);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.status {
		font-size: 9.5px;
		padding: 2px 6px;
		border-radius: 999px;
		flex-shrink: 0;
	}
	.running-badge {
		color: #bae6fd;
		background: rgba(12, 74, 110, 0.6);
	}
	.failed-badge {
		color: #fecaca;
		background: rgba(127, 29, 29, 0.5);
	}
	.play-badge {
		font-size: 9px;
		color: var(--text-primary);
		background: color-mix(in srgb, var(--accent) 22%, transparent);
		border-radius: 50%;
		width: 18px;
		height: 18px;
		display: inline-flex;
		align-items: center;
		justify-content: center;
		flex-shrink: 0;
	}
	.media {
		height: 124px;
		background: var(--bg-elevated);
		display: flex;
		align-items: center;
		justify-content: center;
		overflow: hidden;
	}
	/* SafeMedia renders the <img> inside a child component, so the scoped
	 * `.media-el` below never lands on it — the :global() reach is what
	 * makes it fill the frame (same pattern as EntityNode). Without it the
	 * image renders at natural size and only a slice is visible. */
	.media :global(.media-el),
	.media-el {
		width: 100%;
		height: 100%;
		object-fit: cover;
		display: block;
	}
	.media-btn {
		appearance: none;
		border: none;
		background: transparent;
		padding: 0;
		width: 100%;
		height: 100%;
		cursor: pointer;
		display: block;
	}
	.placeholder {
		font-size: 10px;
		letter-spacing: 0.08em;
		text-transform: uppercase;
		color: var(--text-muted);
	}
	/* Identical to EntityNode's kind badge: transparent-black pill, muted
	 * white text — one style across project and non-project cards. */
	.out-kind {
		position: absolute;
		right: 16px;
		bottom: 10px;
		font-size: 8.5px;
		font-weight: 600;
		letter-spacing: 0.04em;
		text-transform: lowercase;
		color: var(--text-muted);
		pointer-events: none;
		background: rgba(0, 0, 0, 0.55);
		padding: 1px 5px;
		border-radius: 999px;
	}
</style>
